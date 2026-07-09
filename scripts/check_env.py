#!/usr/bin/env python3
"""系统环境全面体检 — 跑在新 notebook 上，把结果贴给我"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

REPORT = {}


def h(title: str):
    print(f"\n{'─' * 55}")
    print(f"  {title}")
    print(f"{'─' * 55}")


def kv(k, v):
    REPORT[k] = v
    print(f"  {k:<30} {v}")


def run(cmd: str) -> str:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        return (r.stdout + r.stderr).strip()
    except Exception as e:
        return str(e)


# ============================================================
# 1. 系统信息
# ============================================================
h("1. 系统信息")
kv("hostname", platform.node())
kv("OS", f"{platform.system()} {platform.release()}")
kv("kernel", platform.version()[:60])
kv("python", sys.version.split()[0])
kv("pip", run("pip --version 2>/dev/null | head -1"))
kv("uv", run("uv --version 2>/dev/null || echo 'NOT INSTALLED'"))
kv("arch", platform.machine())

# Shell / env
kv("SHELL", os.environ.get("SHELL", "?"))
kv("USER", os.environ.get("USER", "?"))
kv("HOME", os.environ.get("HOME", "?"))

# ============================================================
# 2. GPU
# ============================================================
h("2. GPU")
nvidia_output = run(
    "nvidia-smi --query-gpu=index,name,memory.total,memory.free,driver_version,compute_cap --format=csv,noheader 2>&1"
)
print(f"  {nvidia_output}")
REPORT["nvidia_smi"] = nvidia_output

try:
    import torch

    kv("torch_version", torch.__version__)
    kv("cuda_available", str(torch.cuda.is_available()))
    if torch.cuda.is_available():
        kv("cuda_version", torch.version.cuda)
        kv("cudnn_version", str(torch.backends.cudnn.version()))
        kv("gpu_count", torch.cuda.device_count())
        for i in range(torch.cuda.device_count()):
            p = torch.cuda.get_device_properties(i)
            kv(f"  GPU[{i}]", f"{p.name}, {p.total_mem / 1e9:.1f}GB, CC {p.major}.{p.minor}")
except ImportError:
    kv("torch", "NOT INSTALLED")

# ============================================================
# 3. 内存 / 磁盘
# ============================================================
h("3. 内存 & 磁盘")
kv("RAM", run("free -h | grep Mem | awk '{print $2, $3, $7}'"))
kv("CPU cores", os.cpu_count())
kv("disk_root", run("df -h / | tail -1 | awk '{print $2, $3, $5}'"))

# /inspire 共享存储
inspire_path = "/inspire/hdd/project/multi-agent/niexiaohang-25130061"
if os.path.exists(inspire_path):
    kv("inspire_disk", run(f"df -h {inspire_path} | tail -1 | awk '{{print $2, $3, $5}}'"))
    kv("inspire_exists", "YES")
else:
    kv("inspire_exists", "NO - path missing!")
    # Try to find it
    alt = run("find /inspire -maxdepth 3 -name 'scholight' -type d 2>/dev/null | head -3")
    if alt:
        kv("alt_inspire_found", alt)

# ============================================================
# 4. 关键工具
# ============================================================
h("4. 关键工具")
for tool in ["tmux", "htop", "nvtop", "poppler-utils", "git", "curl", "tree"]:
    kv(f"  {tool}", shutil.which(tool) or "NOT INSTALLED")

kv("pdftoppm", shutil.which("pdftoppm") or "NOT INSTALLED")
kv("pdfinfo", shutil.which("pdfinfo") or "NOT INSTALLED")

# ============================================================
# 5. Python 包
# ============================================================
h("5. Python 生态")
for pkg in [
    "marker",
    "marker-pdf",
    "surya-ocr",
    "texify",
    "torch",
    "transformers",
    "pymupdf",
    "Pillow",
    "openai",
    "numpy",
    "tqdm",
]:
    try:
        __import__(pkg.replace("-", "_"))
        ver = getattr(__import__(pkg.replace("-", "_")), "__version__", "?")
        kv(f"  {pkg}", f"✅ {ver}")
    except (ImportError, ModuleNotFoundError):
        kv(f"  {pkg}", "❌ NOT INSTALLED")

# ============================================================
# 6. 项目路径
# ============================================================
h("6. 项目 & 数据")
project = "/inspire/hdd/project/multi-agent/niexiaohang-25130061/scholight"
if os.path.isdir(project):
    pdfs = sorted(Path(project, "data").glob("*.pdf"))
    kv("project_dir", f"✅ {project}")
    kv("pdf_count", len(pdfs))
    for p in pdfs[:5]:
        kv("  pdf", f"{p.name} ({p.stat().st_size / 1e6:.1f}MB)")
    if len(pdfs) > 5:
        kv("  ...", f"+{len(pdfs) - 5} more")
    # mineru outputs
    mds = sorted(Path(project, "data").glob("*.md"))
    jss = sorted(Path(project, "data").glob("*_content_list.json"))
    kv("mineru_md_count", len(mds))
    kv("mineru_json_count", len(jss))
else:
    kv("project_dir", f"❌ {project} NOT FOUND!")
    # search
    alt_find = run("find /inspire -maxdepth 5 -name 'test_marker.py' -type f 2>/dev/null | head -3")
    if alt_find:
        kv("alt_marker_script", alt_find)

# 脚本
script_path = Path(project, "scripts", "test_marker.py")
kv("test_marker_script", "✅ exists" if script_path.exists() else "❌ NOT FOUND")

# ============================================================
# 7. 网络
# ============================================================
h("7. 网络")
kv(
    "huggingface",
    run("curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 https://huggingface.co 2>&1"),
)
kv("pypi", run("curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 https://pypi.org 2>&1"))
kv(
    "github",
    run("curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 https://github.com 2>&1"),
)
kv(
    "modelscope",
    run("curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 https://modelscope.cn 2>&1"),
)

# ============================================================
# 汇总
# ============================================================
print(f"\n{'═' * 55}")
print("  体检完成! 请把以上输出全部复制给我 😊")
print(f"{'═' * 55}\n")

# Save JSON report
report_path = Path(project, "data", "notebook_env_report.json")
try:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(REPORT, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"📝  JSON 报告已保存: {report_path}")
except Exception:
    pass
