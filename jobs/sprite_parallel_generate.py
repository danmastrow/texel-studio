"""Handler for `sprite.parallel_generate` - planned multi-lane sprite painting."""

from __future__ import annotations

import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Iterator, Optional

from pydantic import BaseModel, Field

from . import Event, JobContext, JobHandler, log, progress, result, register_job
from ._runtime import EventBridge, run_in_thread


PixelGrid = list[list[int]]
MAX_CANVAS_DIMENSION = 256


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class Region(BaseModel):
    x1: int
    y1: int
    x2: int
    y2: int


class SpriteTask(BaseModel):
    id: str
    title: str
    description: str
    region: Region
    depends_on: list[str] = Field(default_factory=list)
    lane: str = "worker"
    acceptance: str = ""


class SpritePlan(BaseModel):
    summary: str
    tasks: list[SpriteTask]


class PixelPatch(BaseModel):
    task_id: str
    pixels: list[dict[str, int]] = Field(default_factory=list)
    accepted: int = 0
    rejected: int = 0


class ParallelGenerateParams(BaseModel):
    prompt: str
    colors: list[str] = Field(default_factory=lambda: ["#c8a44e"])
    size: int = 16
    width: Optional[int] = None
    height: Optional[int] = None
    model: Optional[str] = None
    planner_model: Optional[str] = None
    worker_models: list[str] = Field(default_factory=list)
    sprite_type: str = "block"
    system_prompt: Optional[str] = None
    reference_id: Optional[str] = None
    max_lanes: int = 3
    max_steps_per_task: int = 20

    @property
    def canvas_width(self) -> int:
        return max(1, min(MAX_CANVAS_DIMENSION, self.width or self.size))

    @property
    def canvas_height(self) -> int:
        return max(1, min(MAX_CANVAS_DIMENSION, self.height or self.size))

    @property
    def canvas_size(self) -> int:
        return max(self.canvas_width, self.canvas_height)


def blank_pixels(width: int, height: int | None = None) -> PixelGrid:
    canvas_height = height if height is not None else width
    return [[-1] * width for _ in range(canvas_height)]


def copy_pixels(pixels: PixelGrid) -> PixelGrid:
    return [row[:] for row in pixels]


def clamp_region(region: Region, width: int, height: int | None = None) -> Region:
    canvas_height = height if height is not None else width
    x1, x2 = sorted((region.x1, region.x2))
    y1, y2 = sorted((region.y1, region.y2))
    return Region(
        x1=max(0, min(width - 1, x1)),
        y1=max(0, min(canvas_height - 1, y1)),
        x2=max(0, min(width - 1, x2)),
        y2=max(0, min(canvas_height - 1, y2)),
    )


def full_region(width: int, height: int | None = None) -> Region:
    canvas_height = height if height is not None else width
    return Region(x1=0, y1=0, x2=width - 1, y2=canvas_height - 1)


def region_contains(region: Region, x: int, y: int) -> bool:
    return region.x1 <= x <= region.x2 and region.y1 <= y <= region.y2


def diff_pixels(task_id: str, before: PixelGrid, after: PixelGrid, region: Region) -> PixelPatch:
    patch = PixelPatch(task_id=task_id)
    for y, row in enumerate(after):
        for x, color in enumerate(row):
            if before[y][x] == color:
                continue
            if region_contains(region, x, y):
                patch.pixels.append({"x": x, "y": y, "color": int(color)})
            else:
                patch.rejected += 1
    patch.accepted = len(patch.pixels)
    return patch


def merge_patch(canvas: PixelGrid, patch: PixelPatch, width: int, height: int | None = None) -> PixelGrid:
    canvas_height = height if height is not None else width
    merged = copy_pixels(canvas)
    for pixel in patch.pixels:
        x = pixel["x"]
        y = pixel["y"]
        if 0 <= x < width and 0 <= y < canvas_height:
            merged[y][x] = pixel["color"]
    return merged


def _safe_task_id(value: str, fallback: str) -> str:
    task_id = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    return task_id or fallback


def _heuristic_plan(prompt: str, width: int, sprite_type: str, max_lanes: int, height: int | None = None) -> SpritePlan:
    canvas_height = height if height is not None else width
    top_end = max(0, canvas_height // 4)
    mid_start = min(canvas_height - 1, top_end + 1)
    half = max(0, canvas_height // 2 - 1)

    if sprite_type == "block":
        detail_tasks = [
            SpriteTask(
                id="topsoil",
                title="Topsoil and surface material",
                description="Paint the top surface band, material cap, highlights, and any surface growth.",
                region=Region(x1=0, y1=0, x2=width - 1, y2=top_end),
                depends_on=["base"],
                lane="material",
                acceptance="The top band reads as the material surface and stays tile-friendly.",
            ),
            SpriteTask(
                id="body_texture",
                title="Body texture and embedded details",
                description="Add middle and lower texture such as pebbles, roots, cracks, grain, or color variation.",
                region=Region(x1=0, y1=mid_start, x2=width - 1, y2=canvas_height - 1),
                depends_on=["base"],
                lane="texture",
                acceptance="The body has recognizable texture without destroying the base silhouette.",
            ),
        ]
    else:
        detail_tasks = [
            SpriteTask(
                id="upper_details",
                title="Upper form details",
                description="Add upper-half features such as face, top accents, highlights, or readable silhouette cues.",
                region=Region(x1=0, y1=0, x2=width - 1, y2=half),
                depends_on=["base"],
                lane="detail",
                acceptance="The upper form is readable at native pixel size.",
            ),
            SpriteTask(
                id="lower_details",
                title="Lower form details",
                description="Add lower-half features such as legs, base shape, shadow, or lower silhouette details.",
                region=Region(x1=0, y1=half + 1, x2=width - 1, y2=canvas_height - 1),
                depends_on=["base"],
                lane="detail",
                acceptance="The lower form supports the subject and keeps clean transparent edges if needed.",
            ),
        ]

    return SpritePlan(
        summary=f"Task breakdown for {prompt}",
        tasks=[
            SpriteTask(
                id="base",
                title="Base silhouette and large color blocks",
                description="Block in the complete sprite with broad shapes and the main material colors.",
                region=full_region(width, canvas_height),
                lane="base",
                acceptance="The subject reads clearly before details are added.",
            ),
            *detail_tasks[:max_lanes],
            SpriteTask(
                id="polish",
                title="Final polish and cohesion",
                description="Unify edges, contrast, readable details, and any conflicts left by earlier lanes.",
                region=full_region(width, canvas_height),
                depends_on=["base", *[task.id for task in detail_tasks[:max_lanes]]],
                lane="critic",
                acceptance="The final sprite is coherent, clean, and ready to export.",
            ),
        ],
    )


def _extract_json(text: str) -> dict:
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Planner response did not contain JSON")
    return json.loads(text[start:end + 1])


def _plan_prompt(params: ParallelGenerateParams, default_model: str) -> str:
    palette = "\n".join(f"{i}: {color}" for i, color in enumerate(params.colors))
    max_details = max(1, min(params.max_lanes, 4))
    width = params.canvas_width
    height = params.canvas_height
    return f"""Create a procedural pixel-art task plan as strict JSON.

Subject: {params.prompt}
Sprite type: {params.sprite_type}
Canvas: {width}x{height}
Planner model fallback: {default_model}
Palette:
{palette}

Return only JSON with this shape:
{{
  "summary": "one sentence",
  "tasks": [
    {{
      "id": "base",
      "title": "Base silhouette",
      "description": "what to draw procedurally",
      "region": {{"x1": 0, "y1": 0, "x2": {width - 1}, "y2": {height - 1}}},
      "depends_on": [],
      "lane": "base",
      "acceptance": "how to know it is done"
    }}
  ]
}}

Rules:
- Include exactly one base task first.
- Include {max_details} detail/material tasks that depend on "base".
- Include one polish task last that depends on every earlier task.
- Regions must stay inside x=0..{width - 1}, y=0..{height - 1}.
- Use concise task descriptions that tell a worker what pixels to draw.
- Do not include markdown, commentary, or trailing text."""


def _normalize_plan(plan: SpritePlan, prompt: str, width: int, sprite_type: str, max_lanes: int, height: int | None = None) -> SpritePlan:
    canvas_height = height if height is not None else width
    if not plan.tasks:
        return _heuristic_plan(prompt, width, sprite_type, max_lanes, canvas_height)

    seen: set[str] = set()
    normalized: list[SpriteTask] = []
    for index, task in enumerate(plan.tasks):
        task_id = _safe_task_id(task.id or task.title, f"task_{index}")
        if task_id in seen:
            task_id = f"{task_id}_{index}"
        seen.add(task_id)
        normalized.append(task.model_copy(update={
            "id": task_id,
            "region": clamp_region(task.region, width, canvas_height),
        }))

    base = normalized[0].model_copy(update={"id": "base", "depends_on": [], "region": full_region(width, canvas_height)})
    details = [
        task.model_copy(update={"depends_on": task.depends_on or ["base"]})
        for task in normalized[1:]
        if "cleanup" not in task.id and "final" not in task.id and "polish" not in task.id
    ][:max(1, min(max_lanes, 4))]

    polish_candidates = [task for task in normalized[1:] if "cleanup" in task.id or "final" in task.id or "polish" in task.id]
    polish = polish_candidates[0] if polish_candidates else SpriteTask(
        id="polish",
        title="Final polish and cohesion",
        description="Unify the merged sprite, fix conflicts, clean edges, and improve readability.",
        region=full_region(width, canvas_height),
        lane="critic",
        acceptance="The sprite reads clearly and the merged lanes feel cohesive.",
    )
    polish = polish.model_copy(update={
        "id": "polish",
        "title": "Final polish and cohesion",
        "region": full_region(width, canvas_height),
        "depends_on": [task.id for task in [base, *details]],
    })

    return SpritePlan(summary=plan.summary or f"Task breakdown for {prompt}", tasks=[base, *details, polish])


def _build_plan(params: ParallelGenerateParams, default_model: str) -> SpritePlan:
    planner_model = params.planner_model or params.model or default_model
    try:
        from langchain_core.messages import HumanMessage
        from agent import _get_llm

        response = _get_llm(planner_model, temperature=0.2).invoke([HumanMessage(content=_plan_prompt(params, default_model))])
        content = response.content if hasattr(response, "content") else str(response)
        if isinstance(content, list):
            content = "\n".join(str(part) for part in content)
        raw = _extract_json(str(content))
        plan = SpritePlan.model_validate(raw)
    except Exception:
        plan = _heuristic_plan(params.prompt, params.canvas_width, params.sprite_type, params.max_lanes, params.canvas_height)
    return _normalize_plan(plan, params.prompt, params.canvas_width, params.sprite_type, params.max_lanes, params.canvas_height)


def _task_prompt(params: ParallelGenerateParams, plan: SpritePlan, task: SpriteTask) -> str:
    region = task.region
    return f"""{params.prompt}

This is one task in a parallel procedural generation plan.

Overall plan: {plan.summary}
Current task: {task.title}
Task details: {task.description}
Acceptance criteria: {task.acceptance}

Only change pixels in this region:
x={region.x1}..{region.x2}, y={region.y1}..{region.y2}

Keep the rest of the current canvas intact. Use broad procedural tools first,
then small detail tools. Call finish when this task is complete."""


def _worker_model(params: ParallelGenerateParams, index: int, default_model: str) -> str:
    if params.worker_models:
        return params.worker_models[index % len(params.worker_models)]
    return params.model or default_model


def _effective_worker_count(params: ParallelGenerateParams, ready_count: int) -> int:
    requested = max(1, min(params.max_lanes, ready_count, 4))
    models = [model.strip() for model in params.worker_models if model.strip()]
    if not models:
        return 1
    if len(set(models)) == 1:
        return 1
    return min(requested, len(set(models)))


@register_job("sprite.parallel_generate")
class ParallelGenerateHandler(JobHandler):
    Params = ParallelGenerateParams

    def run(self, params: ParallelGenerateParams, ctx: JobContext) -> Iterator[Event]:
        from server import DEFAULT_MODEL, DEFAULT_SYSTEM_PROMPT, load_reference_b64, upscale_image
        from agent import Canvas, run_agent_stream
        import storage

        bridge = EventBridge(timeout_seconds=900.0)
        job_started_at = time.perf_counter()

        def emit_task(event_type: str, task: SpriteTask, **extra) -> None:
            bridge.emit(Event(name="task", data={
                "type": event_type,
                "task_id": task.id,
                "task": task.model_dump(),
                "timestamp": utc_timestamp(),
                "job_elapsed_ms": round((time.perf_counter() - job_started_at) * 1000, 2),
                **extra,
            }))

        def execute_task(task: SpriteTask, index: int, starting_pixels: PixelGrid, plan: SpritePlan) -> PixelPatch:
            if ctx.cancel_check():
                raise RuntimeError("Job canceled by user")

            model = _worker_model(params, index, DEFAULT_MODEL)
            task_started_at = time.perf_counter()
            emit_task("task_started", task, model=model)
            step_count = [0]

            def on_step(canvas, step_type, msg):
                step_count[0] += 1
                if step_type in {"tool_call", "tool_result", "canceled"}:
                    bridge.emit(log(
                        str(msg)[:240],
                        step=f"{task.id}_{step_type}_{step_count[0]}",
                        task_id=task.id,
                    ))

            canvas = run_agent_stream(
                gen_id=f"{ctx.external_id or ctx.job_id}_{task.id}_{uuid.uuid4().hex[:8]}",
                message=_task_prompt(params, plan, task),
                palette=params.colors,
                size=params.canvas_size,
                model_name=model,
                style_prompt=params.system_prompt or DEFAULT_SYSTEM_PROMPT,
                sprite_type=params.sprite_type,
                reference_b64=load_reference_b64(params.reference_id),
                on_step=on_step,
                max_steps=max(4, min(params.max_steps_per_task, 80)),
                existing_pixels=copy_pixels(starting_pixels),
                cancel_check=ctx.cancel_check,
                width=params.canvas_width,
                height=params.canvas_height,
            )
            patch = diff_pixels(task.id, starting_pixels, canvas.pixels, task.region)
            emit_task(
                "task_completed",
                task,
                model=model,
                accepted=patch.accepted,
                rejected=patch.rejected,
                steps=step_count[0],
                duration_ms=round((time.perf_counter() - task_started_at) * 1000, 2),
            )
            return patch

        def worker() -> None:
            external_id = ctx.external_id or ctx.job_id
            width = params.canvas_width
            height = params.canvas_height
            size = params.canvas_size
            canonical = blank_pixels(width, height)
            completed: set[str] = set()
            errors: list[str] = []
            iteration = 0

            bridge.emit(log("Planning parallel sprite tasks...", step="plan_start"))
            plan = _build_plan(params, DEFAULT_MODEL)
            bridge.emit(Event(name="plan", data={
                **plan.model_dump(),
                "timestamp": utc_timestamp(),
                "job_elapsed_ms": round((time.perf_counter() - job_started_at) * 1000, 2),
            }))
            for task in plan.tasks:
                emit_task("task_created", task)

            pending = plan.tasks[:]
            while pending:
                if ctx.cancel_check():
                    bridge.emit(Event(name="canceled", data={"message": "Job canceled by user"}))
                    return

                ready = [task for task in pending if all(dep in completed for dep in task.depends_on)]
                if not ready:
                    ready = [pending[0]]

                starting_pixels = copy_pixels(canonical)
                max_workers = _effective_worker_count(params, len(ready))
                patches: dict[str, PixelPatch] = {}

                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(execute_task, task, index, starting_pixels, plan): task
                        for index, task in enumerate(ready)
                    }
                    for future in as_completed(futures):
                        task = futures[future]
                        try:
                            patches[task.id] = future.result()
                        except Exception as e:
                            errors.append(f"{task.title}: {e}")
                            emit_task("task_error", task, message=str(e))

                for task in ready:
                    patch = patches.get(task.id)
                    if patch is not None:
                        canonical = merge_patch(canonical, patch, width, height)
                        iteration += 1
                        emit_task("patch_accepted", task, accepted=patch.accepted, rejected=patch.rejected)
                        bridge.emit(progress(
                            pixel_data=copy_pixels(canonical),
                            iteration=iteration,
                            notes=f"Merged {task.title}",
                            task_id=task.id,
                            accepted=patch.accepted,
                            rejected=patch.rejected,
                        ))
                    completed.add(task.id)
                    pending = [item for item in pending if item.id != task.id]

            final_img = Canvas(size, params.colors, canonical, width=width, height=height).to_image()
            filename = f"gen_{external_id}_{width}x{height}.png"
            storage.save_image(final_img, f"output/{filename}")
            storage.save_image(upscale_image(final_img, 512), f"output/gen_{external_id}_preview.png")

            status = "completed_with_errors" if errors else "completed"
            bridge.emit(log("Parallel generation complete", step="complete", status=status))
            bridge.emit(result(
                id=external_id,
                image_path=filename,
                iterations=iteration,
                pixel_data=canonical,
                plan=plan.model_dump(),
                errors=errors,
                total_duration_ms=round((time.perf_counter() - job_started_at) * 1000, 2),
                status=status,
            ))

        run_in_thread(worker, bridge)
        yield from bridge.iter_events()
