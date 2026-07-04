#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import shutil
import sys
from pathlib import Path

import flip_evaluator
import numpy as np
from PIL import Image


DEFAULT_REFERENCE_DIR = Path("/home/anders/code/MaterialX/build/glsl-render-tests/flat-glsl-pngs")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def key_from_reference(path: Path) -> str | None:
    suffix = "_glsl.png"
    if path.name.endswith(suffix):
        return path.name[: -len(suffix)]
    return None


def key_from_render(path: Path) -> str | None:
    prefix = "materialx."
    suffix = ".exr"
    if path.name.startswith(prefix) and path.name.endswith(suffix):
        return path.name[len(prefix) : -len(suffix)]
    return None


def read_rgb(path: Path) -> np.ndarray:
    image = np.asarray(flip_evaluator.load(str(path)), dtype=np.float32)
    if image.ndim == 2:
        image = np.stack([image, image, image], axis=-1)
    if image.shape[-1] == 1:
        image = np.repeat(image, 3, axis=-1)
    rgb = image[..., :3]
    rgb = np.nan_to_num(rgb, nan=0.0, posinf=1.0, neginf=0.0)
    return np.clip(rgb, 0.0, None)


def linear_to_srgb(linear: np.ndarray) -> np.ndarray:
    linear = np.clip(linear, 0.0, None)
    return np.where(linear <= 0.0031308, linear * 12.92, 1.055 * np.power(linear, 1.0 / 2.4) - 0.055)


def ldr_rgb_for_path(path: Path, rgb: np.ndarray) -> np.ndarray:
    if path.suffix.lower() == ".exr":
        return np.clip(linear_to_srgb(rgb), 0.0, 1.0)
    return np.clip(rgb, 0.0, 1.0)


def render_preview(path: Path, render_rgb: np.ndarray) -> np.ndarray:
    if path.suffix.lower() == ".exr":
        return linear_to_srgb(render_rgb)
    return np.clip(render_rgb, 0.0, 1.0)


def save_png(path: Path, rgb: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    u8 = (np.clip(rgb, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    Image.fromarray(u8).save(path)


def relpath(path: Path, start: Path) -> str:
    return path.relative_to(start).as_posix()


def build_html(title: str, rows: list[dict[str, object]], generated_dir: Path) -> str:
    means = [float(row["flip_mean"]) for row in rows]
    max_mean = max(means) if means else 0.0
    avg_mean = sum(means) / len(means) if means else 0.0
    worst = sorted(rows, key=lambda row: float(row["flip_mean"]), reverse=True)[:12]

    def esc(value: object) -> str:
        return html.escape(str(value), quote=True)

    row_html = []
    for row in rows:
        key = str(row["key"])
        mean = float(row["flip_mean"])
        row_html.append(
            f"""
            <article class="case" data-key="{esc(key)}" id="{esc(key)}">
              <header class="case-header">
                <div>
                  <h2>{esc(key)}</h2>
                  <p>{esc(row["reference_name"])} -> {esc(row["render_name"])}</p>
                </div>
                <div class="metric">
                  <span>Mean FLIP</span>
                  <strong>{mean:.6f}</strong>
                </div>
              </header>
              <div class="images">
                <figure>
                  <a href="{esc(row["reference_png"])}"><img loading="lazy" src="{esc(row["reference_png"])}" alt="Reference {esc(key)}"></a>
                  <figcaption>MaterialX GLSL reference</figcaption>
                </figure>
                <figure>
                  <a href="{esc(row["render_png"])}"><img loading="lazy" src="{esc(row["render_png"])}" alt="Rendered {esc(key)}"></a>
                  <figcaption>USD render preview</figcaption>
                </figure>
                <figure>
                  <a href="{esc(row["diff_png"])}"><img loading="lazy" src="{esc(row["diff_png"])}" alt="FLIP diff {esc(key)}"></a>
                  <figcaption>FLIP perceptual diff</figcaption>
                </figure>
              </div>
            </article>
            """
        )

    worst_html = "\n".join(
        f'<li><a href="#{esc(row["key"])}">{esc(row["key"])}</a><span>{float(row["flip_mean"]):.6f}</span></li>'
        for row in worst
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #101112;
      --panel: #181a1d;
      --panel-2: #202329;
      --text: #eceff4;
      --muted: #9ba3af;
      --line: #30343c;
      --accent: #6fb7ff;
      --bad: #ff8a6b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .topbar {{
      position: sticky;
      top: 0;
      z-index: 2;
      border-bottom: 1px solid var(--line);
      background: rgba(16, 17, 18, 0.94);
      backdrop-filter: blur(10px);
    }}
    .topbar-inner {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(220px, 360px);
      gap: 24px;
      max-width: 1680px;
      margin: 0 auto;
      padding: 18px 24px;
      align-items: center;
    }}
    h1, h2, p {{ margin: 0; }}
    h1 {{ font-size: 22px; font-weight: 650; }}
    .summary {{
      display: flex;
      flex-wrap: wrap;
      gap: 14px;
      margin-top: 10px;
      color: var(--muted);
    }}
    .summary strong {{ color: var(--text); font-weight: 650; }}
    input[type="search"] {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--text);
      padding: 10px 12px;
      font: inherit;
    }}
    main {{
      max-width: 1680px;
      margin: 0 auto;
      padding: 24px;
    }}
    .worst {{
      margin-bottom: 24px;
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }}
    .worst h2 {{
      font-size: 15px;
      margin-bottom: 12px;
    }}
    .worst ol {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 8px 20px;
      padding-left: 20px;
      margin: 0;
    }}
    .worst li {{
      color: var(--muted);
    }}
    .worst li span {{
      float: right;
      color: var(--bad);
      font-variant-numeric: tabular-nums;
    }}
    .case {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      margin-bottom: 20px;
      overflow: hidden;
    }}
    .case-header {{
      display: flex;
      justify-content: space-between;
      gap: 20px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      background: var(--panel-2);
    }}
    .case-header h2 {{
      font-size: 15px;
      font-weight: 650;
      word-break: break-word;
    }}
    .case-header p {{
      margin-top: 4px;
      color: var(--muted);
      font-size: 12px;
      word-break: break-word;
    }}
    .metric {{
      min-width: 110px;
      text-align: right;
      font-variant-numeric: tabular-nums;
    }}
    .metric span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
    }}
    .metric strong {{
      display: block;
      color: var(--bad);
      font-size: 18px;
      margin-top: 2px;
    }}
    .images {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 1px;
      background: var(--line);
    }}
    figure {{
      margin: 0;
      background: #0b0c0e;
    }}
    img {{
      display: block;
      width: 100%;
      height: auto;
      image-rendering: auto;
      background: #000;
    }}
    figcaption {{
      padding: 10px 12px;
      color: var(--muted);
      background: var(--panel);
      border-top: 1px solid var(--line);
      font-size: 12px;
    }}
    .hidden {{ display: none; }}
    @media (max-width: 960px) {{
      .topbar-inner {{ grid-template-columns: 1fr; }}
      .images {{ grid-template-columns: 1fr; }}
      .case-header {{ flex-direction: column; }}
      .metric {{ text-align: left; }}
    }}
  </style>
</head>
<body>
  <div class="topbar">
    <div class="topbar-inner">
      <div>
        <h1>{esc(title)}</h1>
        <div class="summary">
          <span><strong>{len(rows)}</strong> matched tests</span>
          <span><strong>{avg_mean:.6f}</strong> average mean FLIP</span>
          <span><strong>{max_mean:.6f}</strong> max mean FLIP</span>
        </div>
      </div>
      <label>
        <input id="filter" type="search" placeholder="Filter material tests" autocomplete="off">
      </label>
    </div>
  </div>
  <main>
    <section class="worst">
      <h2>Highest Mean FLIP</h2>
      <ol>
        {worst_html}
      </ol>
    </section>
    {"".join(row_html)}
  </main>
  <script>
    const filter = document.getElementById('filter');
    const cases = Array.from(document.querySelectorAll('.case'));
    filter.addEventListener('input', () => {{
      const needle = filter.value.trim().toLowerCase();
      for (const item of cases) {{
        item.classList.toggle('hidden', needle && !item.dataset.key.toLowerCase().includes(needle));
      }}
    }});
  </script>
</body>
</html>
"""


def parse_args(argv: list[str]) -> argparse.Namespace:
    root = repo_root()
    parser = argparse.ArgumentParser(description="Generate a dark HTML comparison page for MaterialX GLSL PNGs and USD EXR renders.")
    parser.add_argument("--reference-dir", type=Path, default=DEFAULT_REFERENCE_DIR, help="directory containing *_glsl.png reference images")
    parser.add_argument("--render-dir", type=Path, default=root / "materialx" / "renders", help="directory containing materialx.*.exr renders")
    parser.add_argument("--output-dir", type=Path, default=root / "materialx" / "comparison", help="directory to write index.html and generated PNG assets")
    parser.add_argument("--title", default="MaterialX GLSL vs USD Render Comparison")
    parser.add_argument("--limit", type=int, default=0, help="limit number of matched cases, for quick test generation")
    parser.add_argument("--key", action="append", default=[], help="only generate selected test key; may be repeated")
    parser.add_argument("--allow-missing", action="store_true", help="continue if either side has unmatched files")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    reference_dir = args.reference_dir.resolve()
    render_dir = args.render_dir.resolve()
    output_dir = args.output_dir.resolve()

    references = {key_from_reference(path): path for path in sorted(reference_dir.glob("*_glsl.png"))}
    renders = {key_from_render(path): path for path in sorted(render_dir.glob("materialx.*.exr"))}
    references.pop(None, None)
    renders.pop(None, None)

    keys = sorted(set(references) & set(renders))
    if args.key:
        wanted = set(args.key)
        keys = [key for key in keys if key in wanted]
        missing_keys = sorted(wanted - set(keys))
        if missing_keys:
            print(f"requested keys not found: {', '.join(missing_keys)}", file=sys.stderr)
            return 1
    if args.limit:
        keys = keys[: args.limit]

    reference_only = sorted(set(references) - set(renders))
    render_only = sorted(set(renders) - set(references))
    if (reference_only or render_only) and not args.allow_missing:
        print(f"reference-only files: {len(reference_only)}", file=sys.stderr)
        print(f"render-only files: {len(render_only)}", file=sys.stderr)
        print("use --allow-missing to generate the matched subset anyway", file=sys.stderr)
        return 1

    if not keys:
        print("no matching reference/render pairs found", file=sys.stderr)
        return 1

    reference_out = output_dir / "assets" / "reference"
    render_out = output_dir / "assets" / "render"
    diff_out = output_dir / "assets" / "flip"
    for directory in (reference_out, render_out, diff_out):
        directory.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, object]] = []
    for index, key in enumerate(keys, start=1):
        reference_path = references[key]
        render_path = renders[key]
        print(f"[{index:03d}/{len(keys):03d}] {key}")

        reference_png = reference_out / f"{key}.png"
        render_png = render_out / f"{key}.png"
        diff_png = diff_out / f"{key}.png"

        shutil.copy2(reference_path, reference_png)
        reference_rgb = read_rgb(reference_path)
        render_rgb = read_rgb(render_path)
        preview_rgb = render_preview(render_path, render_rgb)
        reference_for_flip = ldr_rgb_for_path(reference_path, reference_rgb)
        render_for_flip = ldr_rgb_for_path(render_path, render_rgb)

        if reference_for_flip.shape[:2] != render_for_flip.shape[:2]:
            print(f"resolution mismatch for {key}: reference {reference_for_flip.shape[:2]} render {render_for_flip.shape[:2]}", file=sys.stderr)
            return 1

        save_png(render_png, preview_rgb)
        flip_map, mean_flip, _ = flip_evaluator.evaluate(reference_for_flip, render_for_flip, "LDR", inputsRGB=True, applyMagma=True, computeMeanError=True)
        save_png(diff_png, np.asarray(flip_map, dtype=np.float32)[..., :3])

        rows.append(
            {
                "key": key,
                "reference_source": str(reference_path),
                "render_source": str(render_path),
                "reference_name": reference_path.name,
                "render_name": render_path.name,
                "reference_png": relpath(reference_png, output_dir),
                "render_png": relpath(render_png, output_dir),
                "diff_png": relpath(diff_png, output_dir),
                "flip_mean": float(mean_flip),
            }
        )

    manifest = {
        "title": args.title,
        "reference_dir": str(reference_dir),
        "render_dir": str(render_dir),
        "count": len(rows),
        "rows": rows,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output_dir / "index.html").write_text(build_html(args.title, rows, output_dir))

    print(f"\nwrote {output_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
