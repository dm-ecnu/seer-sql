# Vendored base: Polygon (PLDI 2025)

- Paper: Pinhan Zhao, Yuepeng Wang, Xinyu Wang. "Polygon: Symbolic Reasoning for SQL using Conflict-Driven
  Under-Approximation Search". PLDI 2025.
- Source: Zenodo record 10.5281/zenodo.15048096 (published 2025-03-20), file `polygon-artifact-evaluation.zip`,
  sha256 `8ad6a0961698a987cacfcd765e208c0ad7ec5302c35830e2098fe1c16bd834fe`.
- License: CC-BY-4.0 (Zenodo record metadata). Attribution: the files under `sql_testing/` are by the Polygon
  authors; the tree is unmodified; SEER ports its encodings into `src/seer/` and `PATCHES.md` lists every kept, fixed and new element.
- Vendored subset: `sql_testing/` (the tool) and the artifact README (`README.upstream.md`). Benchmarks, evaluation
  scripts and the `versions/` ablation copies are not vendored.

The first commit touching this directory is the unmodified upstream snapshot; review our changes with
`git diff <that commit> -- vendor/polygon`.
