#!/usr/bin/env python3
"""
静态资源压缩脚本 —— 在 Docker 构建阶段运行。

用法:
    python compress_assets.py              # 默认路径 /app/web
    python compress_assets.py /app/web_src # 指定路径
    WEB_SRC=/app/web_src python compress_assets.py  # 环境变量
"""
import os
import sys
from pathlib import Path

import htmlmin
import jsmin
from csscompressor import compress as css_compress

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("WEB_SRC", "/app/web"))

print(f"[compress] 目标目录: {ROOT}")

for f in (ROOT / "templates").glob("*.html"):
    original = f.stat().st_size
    f.write_text(htmlmin.minify(f.read_text("utf-8"), remove_comments=True, remove_empty_space=True), "utf-8")
    saved = original - f.stat().st_size
    print(f"  HTML: {f.name}  {original} -> {f.stat().st_size}  (-{saved}B)")

for f in (ROOT / "static/js").glob("*.js"):
    original = f.stat().st_size
    f.write_text(jsmin.jsmin(f.read_text("utf-8")), "utf-8")
    saved = original - f.stat().st_size
    print(f"  JS:   {f.name}  {original} -> {f.stat().st_size}  (-{saved}B)")

for f in (ROOT / "static/css").rglob("*.css"):
    original = f.stat().st_size
    f.write_text(css_compress(f.read_text("utf-8")), "utf-8")
    saved = original - f.stat().st_size
    print(f"  CSS:  {f.name}  {original} -> {f.stat().st_size}  (-{saved}B)")

print("静态资源压缩完成")
