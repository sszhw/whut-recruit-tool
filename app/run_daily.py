#!/usr/bin/env python3
"""每日更新主驱动器：抓取学校网站新增企业招聘 + 对未分析企业做「工作地流动」分析。

由 check_preach_daily.bat 调用。本脚本统一以 UTF-8（含 BOM）写日志，
避免 .bat 的 echo 用 GBK、Python 用 UTF-8 导致的编码混杂乱码。

流程（依次运行，输出全部追加到 <项目根>/preach_update_log.txt）：
    1. check_recruit_update.py —— 学校网站新增的企业招聘信息 / 双选会（增量合并写回原始 JSON）
    2. check_preach_update.py  —— 宣讲会更新；其内部 refresh_work_flow() 会对「未分析企业」
       增量做工作地流动（轨迹流动）推断，并刷新《宣讲会_工作地流动.csv》

退出码：0 = 成功；非 0 = 其中某一步出错（仍继续跑后续步骤）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)
LOG = DATA / "preach_update_log.txt"
PY = sys.executable


def decode_lenient(line: bytes) -> str:
    """容错解码：UTF-8 优先，失败退回 GBK（兼容历史里 .bat echo 用 GBK 写入的日期行）。"""
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return line.decode("gbk", errors="replace")
        except Exception:
            return line.decode("utf-8", errors="replace")


def read_old_log() -> str:
    """读取历史日志并尽量还原可读内容（逐行 utf-8→gbk 容错）。"""
    if not LOG.exists():
        return ""
    data = LOG.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):  # 去掉已有 BOM
        data = data[3:]
    lines = data.splitlines()
    return "\n".join(decode_lenient(line) for line in lines)


def run(script: str, label: str, buf: list[str]) -> int:
    buf.append(f"---- {label} ----")
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run([PY, str(SCRIPT_DIR / script)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              cwd=str(SCRIPT_DIR), env=env)
    except Exception as exc:
        buf.append(f"!!! 无法启动 {script}：{exc}")
        return 2
    text = proc.stdout.decode("utf-8", errors="replace").rstrip("\n") if proc.stdout else ""
    if text:
        buf.append(text)
    if proc.returncode != 0:
        buf.append(f"!!! {script} 退出码 {proc.returncode}")
    return proc.returncode


def main() -> int:
    old = read_old_log().rstrip("\n")
    buf: list[str] = []
    if old:
        buf.append(old)
        buf.append("")
    buf.append(f"==== {datetime.now().strftime('%Y/%m/%d %H:%M:%S')} (每日更新) ====")

    rc_recruit = run("check_recruit_update.py", "学校网站新增：招聘信息 / 双选会更新", buf)
    rc_preach = run("check_preach_update.py", "宣讲会更新（含未分析企业的工作地流动分析）", buf)

    content = "\n".join(buf) + "\n"
    # 统一写 UTF-8 + BOM，记事本/别处查看都不会再乱码
    LOG.write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))
    print(f"日志已更新：{LOG}")
    return 0 if (rc_recruit == 0 and rc_preach == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
