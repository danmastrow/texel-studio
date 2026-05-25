import unittest

from jobs.sprite_parallel_generate import (
    ParallelGenerateParams,
    Region,
    blank_pixels,
    clamp_region,
    diff_pixels,
    merge_patch,
    _effective_worker_count,
    _heuristic_plan,
)


class ParallelGeneratePatchTests(unittest.TestCase):
    def test_diff_rejects_pixels_outside_task_region(self):
        before = blank_pixels(4)
        after = blank_pixels(4)
        after[1][1] = 2
        after[3][3] = 3

        patch = diff_pixels("detail", before, after, Region(x1=0, y1=0, x2=2, y2=2))

        self.assertEqual(patch.accepted, 1)
        self.assertEqual(patch.rejected, 1)
        self.assertEqual(patch.pixels, [{"x": 1, "y": 1, "color": 2}])

    def test_merge_patch_applies_sparse_pixels_without_mutating_source(self):
        canvas = blank_pixels(4)
        after = blank_pixels(4)
        after[0][0] = 1
        after[2][2] = 4
        patch = diff_pixels("base", canvas, after, Region(x1=0, y1=0, x2=3, y2=3))

        merged = merge_patch(canvas, patch, 4)

        self.assertEqual(canvas[0][0], -1)
        self.assertEqual(merged[0][0], 1)
        self.assertEqual(merged[2][2], 4)

    def test_rectangular_patch_merge_uses_width_and_height(self):
        canvas = blank_pixels(6, 3)
        after = blank_pixels(6, 3)
        after[2][5] = 3
        patch = diff_pixels("wide", canvas, after, Region(x1=0, y1=0, x2=5, y2=2))

        merged = merge_patch(canvas, patch, 6, 3)

        self.assertEqual(len(merged), 3)
        self.assertEqual(len(merged[0]), 6)
        self.assertEqual(merged[2][5], 3)

    def test_clamp_region_keeps_bounds_inside_canvas(self):
        region = clamp_region(Region(x1=10, y1=-3, x2=-2, y2=9), 8)

        self.assertEqual(region, Region(x1=0, y1=0, x2=7, y2=7))

    def test_rectangular_clamp_region_uses_independent_axes(self):
        region = clamp_region(Region(x1=10, y1=-3, x2=-2, y2=9), 6, 3)

        self.assertEqual(region, Region(x1=0, y1=0, x2=5, y2=2))

    def test_heuristic_plan_creates_polish_after_parallel_tasks(self):
        plan = _heuristic_plan("mossy dirt", 16, "block", 2)
        ids = [task.id for task in plan.tasks]

        self.assertEqual(ids, ["base", "topsoil", "body_texture", "polish"])
        self.assertEqual(plan.tasks[0].depends_on, [])
        self.assertEqual(plan.tasks[-1].depends_on, ["base", "topsoil", "body_texture"])

    def test_heuristic_plan_supports_rectangular_dimensions(self):
        plan = _heuristic_plan("wide banner", 24, "freeform", 2, 12)

        self.assertEqual(plan.tasks[0].region, Region(x1=0, y1=0, x2=23, y2=11))
        self.assertEqual(plan.tasks[-1].region, Region(x1=0, y1=0, x2=23, y2=11))

    def test_parallel_params_allow_128_by_192(self):
        params = ParallelGenerateParams(prompt="tall tree", width=128, height=192)

        self.assertEqual(params.canvas_width, 128)
        self.assertEqual(params.canvas_height, 192)
        self.assertEqual(params.canvas_size, 192)

    def test_single_worker_model_serializes_lanes(self):
        params = ParallelGenerateParams(
            prompt="stone",
            max_lanes=4,
            worker_models=["same-model"],
        )

        self.assertEqual(_effective_worker_count(params, ready_count=4), 1)

    def test_distinct_worker_models_can_run_parallel(self):
        params = ParallelGenerateParams(
            prompt="stone",
            max_lanes=4,
            worker_models=["fast-model", "slow-model"],
        )

        self.assertEqual(_effective_worker_count(params, ready_count=4), 2)


if __name__ == "__main__":
    unittest.main()
