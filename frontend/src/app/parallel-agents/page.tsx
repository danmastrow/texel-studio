"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, apiRaw, imageUrl } from "@/lib/api";
import { readEventStream, type StreamEvent } from "@/lib/event-stream";
import type { Palette, Settings } from "@/lib/types";

interface Region {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

interface PlanTask {
  id: string;
  title: string;
  description: string;
  region: Region;
  depends_on: string[];
  lane: string;
  acceptance: string;
}

interface TaskState extends PlanTask {
  status: "planned" | "running" | "done" | "merged" | "error";
  model?: string;
  accepted?: number;
  rejected?: number;
  message?: string;
  createdAt?: string;
  startedAt?: string;
  completedAt?: string;
  durationMs?: number;
  jobElapsedMs?: number;
}

interface PlanPayload {
  summary?: string;
  tasks?: PlanTask[];
  timestamp?: string;
  job_elapsed_ms?: number;
}

interface JobPayload {
  id?: string;
}

interface LogPayload {
  step?: string;
  message?: string;
  task_id?: string;
  status?: string;
}

interface ProgressPayload {
  pixel_data?: number[][];
  iteration?: number;
  notes?: string;
  task_id?: string;
  accepted?: number;
  rejected?: number;
}

interface TaskPayload {
  type?: string;
  task_id?: string;
  task?: PlanTask;
  model?: string;
  accepted?: number;
  rejected?: number;
  message?: string;
  steps?: number;
  timestamp?: string;
  duration_ms?: number;
  job_elapsed_ms?: number;
}

interface ResultPayload {
  id?: string;
  image_path?: string;
  pixel_data?: number[][];
  status?: string;
  errors?: string[];
  plan?: PlanPayload;
  total_duration_ms?: number;
}

const DEFAULT_PROMPT = "a mossy dirt block with topsoil, embedded pebbles, and thin root fragments";
const MAX_CANVAS_DIMENSION = 256;

export default function ParallelAgentsPage() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [palettes, setPalettes] = useState<Palette[]>([]);
  const [paletteId, setPaletteId] = useState<number | null>(null);
  const [prompt, setPrompt] = useState(DEFAULT_PROMPT);
  const [width, setWidth] = useState(16);
  const [height, setHeight] = useState(16);
  const [spriteType, setSpriteType] = useState("block");
  const [plannerModel, setPlannerModel] = useState("");
  const [workerModels, setWorkerModels] = useState("");
  const [maxLanes, setMaxLanes] = useState(1);
  const [maxStepsPerTask, setMaxStepsPerTask] = useState(20);
  const [pixels, setPixels] = useState<number[][] | null>(null);
  const [tasks, setTasks] = useState<TaskState[]>([]);
  const [planSummary, setPlanSummary] = useState("");
  const [logs, setLogs] = useState<LogPayload[]>([]);
  const [status, setStatus] = useState<"idle" | "loading" | "running" | "complete" | "error">("loading");
  const [statusMessage, setStatusMessage] = useState("loading settings...");
  const [jobId, setJobId] = useState<string | null>(null);
  const [imagePath, setImagePath] = useState<string | null>(null);
  const [runStartedAt, setRunStartedAt] = useState<number | null>(null);
  const [runFinishedAt, setRunFinishedAt] = useState<number | null>(null);
  const [now, setNow] = useState(Date.now());
  const abortRef = useRef<AbortController | null>(null);

  const currentPalette = useMemo(
    () => palettes.find((palette) => palette.id === paletteId) ?? palettes[0] ?? null,
    [paletteId, palettes],
  );

  useEffect(() => {
    let mounted = true;
    Promise.all([api<Settings>("/settings"), api<Palette[]>("/palettes")])
      .then(([settingsResult, paletteResult]) => {
        if (!mounted) return;
        setSettings(settingsResult);
        setPalettes(paletteResult);
        setPaletteId(paletteResult[0]?.id ?? null);
        setPlannerModel(settingsResult.default_model);
        setWorkerModels(settingsResult.default_model);
        setSpriteType(Object.keys(settingsResult.sprite_types)[0] ?? "block");
        setStatus("idle");
        setStatusMessage("ready");
      })
      .catch((error: Error) => {
        if (!mounted) return;
        setStatus("error");
        setStatusMessage(error.message);
      });

    return () => {
      mounted = false;
      abortRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (status !== "running") return;
    const timer = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(timer);
  }, [status]);

  const resetRun = () => {
    setPixels(Array.from({ length: height }, () => Array(width).fill(-1)));
    setTasks([]);
    setPlanSummary("");
    setLogs([]);
    setImagePath(null);
    setJobId(null);
    setRunStartedAt(null);
    setRunFinishedAt(null);
  };

  const upsertTask = (task: PlanTask, patch: Partial<TaskState> = {}) => {
    setTasks((prev) => {
      const index = prev.findIndex((item) => item.id === task.id);
      const cleanPatch = compactPatch(patch);
      const nextTask: TaskState = {
        ...task,
        status: cleanPatch.status ?? "planned",
        ...cleanPatch,
      };
      if (index < 0) return [...prev, nextTask];
      const next = [...prev];
      next[index] = { ...next[index], ...task, ...cleanPatch };
      return next;
    });
  };

  const updateTask = (taskId: string, patch: Partial<TaskState>) => {
    const cleanPatch = compactPatch(patch);
    setTasks((prev) => prev.map((task) => (
      task.id === taskId ? { ...task, ...cleanPatch } : task
    )));
  };

  const handleStreamEvent = ({ event, data }: StreamEvent) => {
    if (event === "job") {
      const payload = data as JobPayload;
      if (payload.id) setJobId(payload.id);
      return;
    }

    if (event === "plan") {
      const payload = data as PlanPayload;
      setPlanSummary(payload.summary ?? "");
      setTasks((payload.tasks ?? []).map((task) => ({
        ...task,
        status: "planned",
        createdAt: payload.timestamp,
        jobElapsedMs: payload.job_elapsed_ms,
      })));
      return;
    }

    if (event === "task") {
      const payload = data as TaskPayload;
      const task = payload.task;
      if (!task && !payload.task_id) return;

      if (task) {
        const status = taskStatusFor(payload.type);
        upsertTask(task, {
          status,
          model: payload.model,
          accepted: payload.accepted,
          rejected: payload.rejected,
          message: payload.message,
          createdAt: payload.type === "task_created" ? payload.timestamp : undefined,
          startedAt: payload.type === "task_started" ? payload.timestamp : undefined,
          completedAt: payload.type === "task_completed" || payload.type === "patch_accepted" ? payload.timestamp : undefined,
          durationMs: payload.duration_ms,
          jobElapsedMs: payload.job_elapsed_ms,
        });
      } else if (payload.task_id) {
        updateTask(payload.task_id, {
          status: taskStatusFor(payload.type),
          model: payload.model,
          accepted: payload.accepted,
          rejected: payload.rejected,
          message: payload.message,
          completedAt: payload.type === "patch_accepted" ? payload.timestamp : undefined,
          durationMs: payload.duration_ms,
          jobElapsedMs: payload.job_elapsed_ms,
        });
      }
      return;
    }

    if (event === "log") {
      const payload = data as LogPayload;
      setLogs((prev) => [...prev.slice(-79), payload]);
      if (payload.message) setStatusMessage(payload.message);
      return;
    }

    if (event === "progress") {
      const payload = data as ProgressPayload;
      if (payload.pixel_data) setPixels(payload.pixel_data);
      if (payload.notes) setStatusMessage(payload.notes);
      if (payload.task_id) {
        updateTask(payload.task_id, {
          accepted: payload.accepted,
          rejected: payload.rejected,
        });
      }
      return;
    }

    if (event === "result") {
      const payload = data as ResultPayload;
      if (payload.pixel_data) setPixels(payload.pixel_data);
      if (payload.image_path) setImagePath(payload.image_path);
      if (payload.plan?.tasks) {
        setTasks((prev) => mergePlanTasks(prev, payload.plan?.tasks ?? []));
      }
      setStatus(payload.status === "completed_with_errors" ? "error" : "complete");
      setStatusMessage(payload.errors?.length ? payload.errors.join("; ") : "parallel generation complete");
      setRunFinishedAt(Date.now());
      return;
    }

    if (event === "error" || event === "canceled") {
      const payload = data as LogPayload;
      setStatus("error");
      setStatusMessage(payload.message ?? event);
      setRunFinishedAt(Date.now());
    }
  };

  const startGeneration = async () => {
    if (!settings || !currentPalette || !prompt.trim()) return;

    abortRef.current?.abort();
    abortRef.current = new AbortController();
    resetRun();
    setRunStartedAt(Date.now());
    setRunFinishedAt(null);
    setStatus("running");
    setStatusMessage("starting parallel job...");

    const parsedWorkerModels = workerModels
      .split(",")
      .map((model) => model.trim())
      .filter(Boolean);

    try {
      const response = await apiRaw("/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: abortRef.current.signal,
        body: JSON.stringify({
          kind: "sprite.parallel_generate",
          params: {
            prompt: prompt.trim(),
            colors: currentPalette.colors,
            size: Math.max(width, height),
            width,
            height,
            model: parsedWorkerModels[0] || settings.default_model,
            planner_model: plannerModel || settings.default_model,
            worker_models: parsedWorkerModels,
            sprite_type: spriteType,
            max_lanes: maxLanes,
            max_steps_per_task: maxStepsPerTask,
            system_prompt: settings.system_prompt,
          },
        }),
      });

      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || `HTTP ${response.status}`);
      }

      await readEventStream(response, handleStreamEvent);
      abortRef.current = null;
    } catch (error) {
      if ((error as Error).name === "AbortError") {
        setStatus("idle");
        setStatusMessage("canceled");
        setRunFinishedAt(Date.now());
        return;
      }
      setStatus("error");
      setStatusMessage((error as Error).message);
      setRunFinishedAt(Date.now());
    }
  };

  const cancelGeneration = () => {
    abortRef.current?.abort();
    abortRef.current = null;
  };

  return (
    <main className="h-screen overflow-y-auto" style={{ background: "var(--bg)", color: "var(--text)" }}>
      <div className="mx-auto flex min-h-screen max-w-7xl flex-col gap-4 px-5 py-5 md:px-8 md:py-8">
        <header className="flex flex-col gap-4 border-b pb-5 md:flex-row md:items-end md:justify-between" style={{ borderColor: "var(--border)" }}>
          <div>
            <div className="mb-3 flex items-center gap-2">
              <div style={{ width: 7, height: 7, background: "var(--accent)", transform: "rotate(45deg)" }} />
              <span className="label" style={{ marginBottom: 0 }}>
                Experimental Generator
              </span>
            </div>
            <h1 className="text-2xl font-semibold tracking-tight md:text-4xl">Parallel procedural agents</h1>
            <p className="mt-3 max-w-3xl" style={{ color: "var(--text-dim)" }}>
              Plan the sprite as tasks, run bounded model lanes, then merge accepted pixel patches into one canonical canvas.
            </p>
          </div>
          <Link className="btn" href="/">
            Back to studio
          </Link>
        </header>

        <section className="grid gap-4 lg:grid-cols-[320px_1fr_360px]">
          <Panel title="Generate">
            <div className="grid gap-3">
              <textarea
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                rows={5}
                placeholder="describe a sprite..."
              />

              <Field label="palette">
                <select value={paletteId ?? ""} onChange={(event) => setPaletteId(Number(event.target.value))}>
                  {palettes.map((palette) => (
                    <option key={palette.id} value={palette.id}>
                      {palette.name} ({palette.colors.length})
                    </option>
                  ))}
                </select>
              </Field>
              <PaletteSwatches palette={currentPalette} />

              <div className="grid grid-cols-3 gap-2">
                <Field label="type">
                  <select value={spriteType} onChange={(event) => setSpriteType(event.target.value)}>
                    {Object.entries(settings?.sprite_types ?? {}).map(([key, value]) => (
                      <option key={key} value={key}>
                        {value.label}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="width">
                  <DimensionInput value={width} onChange={setWidth} />
                </Field>
                <Field label="height">
                  <DimensionInput value={height} onChange={setHeight} />
                </Field>
              </div>

              <Field label="planner model">
                <select value={plannerModel} onChange={(event) => setPlannerModel(event.target.value)}>
                  {(settings?.models ?? []).map((model) => (
                    <option key={model} value={model}>
                      {model}
                    </option>
                  ))}
                </select>
              </Field>

              <Field label="worker models">
                <input
                  type="text"
                  value={workerModels}
                  onChange={(event) => setWorkerModels(event.target.value)}
                  placeholder="comma-separated model names"
                />
              </Field>

              <Field label="max lanes">
                <select value={maxLanes} onChange={(event) => setMaxLanes(Number(event.target.value))}>
                  {[1, 2, 3, 4].map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
              </Field>

              <Field label="steps per task">
                <DimensionInput value={maxStepsPerTask} min={4} max={80} onChange={setMaxStepsPerTask} />
              </Field>

              <div className="flex gap-2">
                <button className="btn btn-primary flex-1" onClick={startGeneration} disabled={status === "running" || status === "loading"}>
                  {status === "running" ? "running..." : "run parallel job"}
                </button>
                <button className="btn" onClick={cancelGeneration} disabled={status !== "running"}>
                  cancel
                </button>
              </div>
            </div>
          </Panel>

          <Panel title="Canvas">
            <div className="mb-3 flex items-center justify-between gap-3">
              <div style={{ color: "var(--text-dim)" }}>
                {statusMessage}
                {jobId ? <span style={{ color: "var(--text-faint)" }}> - {jobId.slice(0, 8)}</span> : null}
                {runStartedAt ? (
                  <span style={{ color: "var(--accent)" }}> - {formatMs((runFinishedAt ?? now) - runStartedAt)}</span>
                ) : null}
              </div>
              {imagePath ? (
                <a className="btn" href={imageUrl(imagePath)} target="_blank" rel="noreferrer">
                  open png
                </a>
              ) : null}
            </div>
            <PixelPreview pixels={pixels} palette={currentPalette} width={width} height={height} />
          </Panel>

          <div className="grid gap-4">
            <Panel title="Task Board">
              {planSummary ? (
                <p className="mb-3" style={{ color: "var(--text-dim)" }}>{planSummary}</p>
              ) : null}
              <div className="grid gap-2">
                {tasks.length === 0 ? (
                  <p style={{ color: "var(--text-faint)" }}>waiting for planner...</p>
                ) : (
                  tasks.map((task) => <TaskCard key={task.id} task={task} now={now} />)
                )}
              </div>
            </Panel>

            <Panel title="Agent Log">
              <div className="max-h-[260px] overflow-y-auto">
                {logs.length === 0 ? (
                  <p style={{ color: "var(--text-faint)" }}>no events yet</p>
                ) : (
                  logs.map((entry, index) => (
                    <div key={`${entry.step}-${index}`} className="border-b py-1" style={{ borderColor: "var(--border)" }}>
                      <span style={{ color: "var(--accent)" }}>{entry.step ?? "log"}</span>{" "}
                      <span style={{ color: "var(--text-dim)" }}>{entry.message}</span>
                    </div>
                  ))
                )}
              </div>
            </Panel>
          </div>
        </section>
      </div>
    </main>
  );
}

function taskStatusFor(type?: string): TaskState["status"] {
  switch (type) {
    case "task_started":
      return "running";
    case "task_completed":
      return "done";
    case "patch_accepted":
      return "merged";
    case "task_error":
      return "error";
    default:
      return "planned";
  }
}

function compactPatch(patch: Partial<TaskState>): Partial<TaskState> {
  return Object.fromEntries(Object.entries(patch).filter(([, value]) => value !== undefined)) as Partial<TaskState>;
}

function mergePlanTasks(current: TaskState[], tasks: PlanTask[]) {
  return tasks.map((task) => current.find((item) => item.id === task.id) ?? { ...task, status: "planned" as const });
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="border p-4" style={{ borderColor: "var(--border)", background: "rgba(18,17,15,0.72)" }}>
      <h2 className="label">{title}</h2>
      {children}
    </section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <div style={{ fontSize: "9px", color: "var(--text-faint)", marginBottom: 3 }}>{label}</div>
      {children}
    </label>
  );
}

function DimensionInput({
  value,
  onChange,
  min = 4,
  max = MAX_CANVAS_DIMENSION,
}: {
  value: number;
  onChange: (value: number) => void;
  min?: number;
  max?: number;
}) {
  return (
    <input
      type="number"
      min={min}
      max={max}
      step={1}
      value={value}
      onChange={(event) => onChange(clampNumber(Number(event.target.value), min, max))}
      style={{
        width: "100%",
        background: "var(--surface)",
        border: "1px solid var(--border)",
        color: "var(--text)",
        padding: "5px 8px",
        fontFamily: "inherit",
        fontSize: "11px",
      }}
    />
  );
}

function clampNumber(value: number, min: number, max: number) {
  if (!Number.isFinite(value)) return 16;
  return Math.max(min, Math.min(max, Math.round(value)));
}

function PaletteSwatches({ palette }: { palette: Palette | null }) {
  if (!palette) {
    return null;
  }

  return (
    <div>
      <div className="mb-1 flex items-center justify-between" style={{ fontSize: "9px", color: "var(--text-faint)" }}>
        <span>{palette.name}</span>
        <span>{palette.colors.length} colors</span>
      </div>
      <div className="flex max-h-[96px] flex-wrap gap-[2px] overflow-y-auto border p-1" style={{ borderColor: "var(--border)", background: "var(--bg)" }}>
        {palette.colors.map((color, index) => (
          <div
            key={`${color}-${index}`}
            title={`${index}: ${color}`}
            className="relative"
            style={{
              width: 20,
              height: 20,
              background: color,
              outline: "1px solid var(--border)",
            }}
          >
            <span
              className="absolute bottom-0 right-[1px]"
              style={{
                color: readableTextColor(color),
                fontSize: "7px",
                lineHeight: 1,
                textShadow: "0 1px 1px rgba(0,0,0,0.45)",
              }}
            >
              {index < 10 ? index : ""}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function readableTextColor(hex: string) {
  const normalized = hex.replace("#", "");
  if (normalized.length !== 6) return "#fff";
  const r = parseInt(normalized.slice(0, 2), 16);
  const g = parseInt(normalized.slice(2, 4), 16);
  const b = parseInt(normalized.slice(4, 6), 16);
  return r * 0.299 + g * 0.587 + b * 0.114 > 150 ? "#111" : "#fff";
}

function PixelPreview({ pixels, palette, width, height }: { pixels: number[][] | null; palette: Palette | null; width: number; height: number }) {
  const grid = pixels ?? Array.from({ length: height }, () => Array(width).fill(-1));
  const colors = palette?.colors ?? [];

  return (
    <div
      className="mx-auto grid w-full max-w-[640px] border"
      style={{
        borderColor: "var(--border)",
        gridTemplateColumns: `repeat(${width}, minmax(0, 1fr))`,
        imageRendering: "pixelated",
      }}
    >
      {grid.flatMap((row, y) => row.map((colorIndex, x) => {
        const color = colorIndex >= 0 ? colors[colorIndex] : undefined;
        return (
          <div
            key={`${x}-${y}`}
            title={`${x},${y}: ${colorIndex}`}
            style={{
              aspectRatio: "1",
              background: color ?? ((x + y) % 2 === 0 ? "var(--canvas-light)" : "var(--canvas-dark)"),
              boxShadow: Math.max(width, height) <= 32 ? "inset 0 0 0 1px rgba(255,255,255,0.025)" : undefined,
            }}
          />
        );
      }))}
    </div>
  );
}

function TaskCard({ task, now }: { task: TaskState; now: number }) {
  const runningMs = task.startedAt && task.status === "running" ? now - Date.parse(task.startedAt) : undefined;
  const displayDuration = task.durationMs ?? runningMs;

  return (
    <div className="border p-3" style={{ borderColor: "var(--border)", background: "var(--bg)" }}>
      <div className="mb-1 flex items-start justify-between gap-2">
        <h3 className="font-semibold" style={{ color: "var(--text)" }}>{task.title}</h3>
        <span className="border px-2 py-1 text-[10px] uppercase tracking-[0.12em]" style={{ borderColor: "var(--border)", color: statusColor(task.status) }}>
          {task.status}
        </span>
      </div>
      <p style={{ color: "var(--text-dim)" }}>{task.description}</p>
      <div className="mt-2 grid grid-cols-2 gap-2" style={{ color: "var(--text-faint)" }}>
        <span>{task.lane}</span>
        <span>
          {task.region.x1},{task.region.y1} to {task.region.x2},{task.region.y2}
        </span>
        <span>{task.model ?? "model pending"}</span>
        <span>
          {task.accepted ?? 0} accepted / {task.rejected ?? 0} rejected
        </span>
        <span>{task.createdAt ? formatTime(task.createdAt) : "not queued"}</span>
        <span>{displayDuration !== undefined ? formatMs(displayDuration) : "no duration yet"}</span>
      </div>
      {task.message ? <p className="mt-2" style={{ color: "var(--danger)" }}>{task.message}</p> : null}
    </div>
  );
}

function statusColor(status: TaskState["status"]) {
  if (status === "error") return "var(--danger)";
  if (status === "merged" || status === "done") return "var(--success)";
  if (status === "running") return "var(--accent)";
  return "var(--text-faint)";
}

function formatTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString();
}

function formatMs(value: number) {
  if (!Number.isFinite(value)) return "0.0s";
  if (value < 1000) return `${Math.max(0, Math.round(value))}ms`;
  const seconds = value / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.floor(seconds % 60).toString().padStart(2, "0");
  return `${minutes}:${remainder}`;
}
