"""地形描边与最终地形封面的视觉回归检查（全部使用合成数据，无需源视频）。"""
import sys
import unittest
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from make_cover import final_terrain
from make_video import level_map, palette_interp, shade_terrain


class RenderingTests(unittest.TestCase):
    def test_dense_palette_has_400_distinct_colors(self):
        palette = palette_interp(400)
        self.assertEqual(len(np.unique(palette, axis=0)), 400)
        self.assertLessEqual(int(np.abs(np.diff(palette.astype(int), axis=0)).max()), 5)

    def test_gradient_can_change_color_without_a_line_at_every_level(self):
        counts = np.arange(1, 401, dtype=np.uint16)[None, :]
        bounds = np.linspace(1, 400, 401)[1:-1]
        palette = palette_interp(400)
        actual = shade_terrain(counts, bounds, False, palette, 0, fine_contours=False)
        self.assertTrue(np.array_equal(actual, palette[level_map(counts, bounds)]))

    def test_real_terrain_contours_do_not_turn_into_black_bands(self):
        counts = np.load(ROOT / "tests/fixtures/counts_110.npy")
        bounds = np.linspace(1, 400, 25)[1:-1]
        palette = palette_interp(24)
        field = cv2.GaussianBlur(counts.astype(np.float32), (3, 3), 0)
        base = palette[level_map(field, bounds)]
        actual = shade_terrain(counts, bounds, True, palette, 4)
        ratio = actual.sum(axis=2) / base.sum(axis=2)
        self.assertGreaterEqual(float(ratio.min()), 0.72)
        self.assertTrue(np.any(actual != base), "等高线仍应可见")

    def test_a_straight_major_contour_is_only_one_source_pixel_wide(self):
        counts = np.full((20, 20), 45, np.uint16)
        counts[:, 10:] = 55
        bounds = np.arange(10, 120, 10)
        palette = palette_interp(12)
        base = palette[level_map(counts, bounds)]
        actual = shade_terrain(counts, bounds, False, palette, 4)
        changed = np.any(actual != base, axis=2)
        self.assertTrue(np.all(changed.sum(axis=1) == 1))

    def test_zero_level_boundary_is_not_a_major_contour(self):
        counts = np.zeros((4, 4), np.uint16)
        counts[:, 2:] = 15
        bounds = np.arange(10, 120, 10)
        palette = palette_interp(12)
        actual = shade_terrain(counts, bounds, False, palette, 4)
        self.assertTrue(np.array_equal(actual[:, 1],
                                       np.tile(np.rint(palette[0] * 0.90).astype(np.uint8), (4, 1))))

    def test_crossing_a_major_height_uses_the_major_contour(self):
        counts = np.full((4, 4), 35, np.uint16)
        counts[:, 2:] = 45
        palette = palette_interp(12)
        actual = shade_terrain(counts, bounds=np.arange(10, 120, 10),
                               smooth=False, palette=palette, major_every=4)
        self.assertTrue(np.array_equal(actual[:, 1],
                                       np.tile(np.rint(palette[3] * 0.78).astype(np.uint8), (4, 1))))

    def test_final_terrain_uses_fixed_linear_scale(self):
        rng = np.random.default_rng(7)
        counts = rng.integers(1, 201, size=(50, 60)).astype(np.uint16)
        n_levels = 12
        bounds = np.linspace(1, counts.max(), n_levels + 1)[1:-1].astype(np.float32)
        expected = shade_terrain(counts, bounds, True, palette_interp(n_levels),
                                 n_levels // 6)
        actual = final_terrain(counts, n_levels)
        self.assertEqual(actual.shape, counts.shape + (3,))
        self.assertTrue(np.array_equal(actual, expected))


if __name__ == "__main__":
    unittest.main()
