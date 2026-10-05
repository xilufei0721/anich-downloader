#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AniCh 番剧资源抓取工具(纯 Python 标准库,无需第三方依赖)

从 AniCh(https://github.com/Sle2p/AniCh)的公开 API 抓取番剧信息与视频线路,
配合 N_m3u8DL-RE / yt-dlp 下载视频,供个人学习与混剪使用。

用法示例:
    python anich_dl.py search "葬送的芙莉莲"
    python anich_dl.py episodes <番剧ID>
    python anich_dl.py play <番剧ID> 1
    python anich_dl.py download <番剧ID> 1 --line 1
"""

import argparse
import base64
import datetime
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


USER_AGENT = "cx.xs.open Android 1.0.0"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
TIMEOUT = 12

# 主 API 与备用节点,自动轮换(优先存活节点)
BASE_URLS = [
    "https://anich.sends.eu.org",
    "https://api.500403.xyz",
    "https://ani.emmmm.eu.org",
    "https://api.emmmm.eu.org.cdn.cloudflare.net",
    "https://api.emmmm.eu.org",
]


def deobfuscate_url(value):
    """AniCh 新版混淆 base64:在标准 base64 里插入 'A0' 字符。
    先去掉 'A0' 再解码;解码结果以 http 开头才采用,否则返回原值。"""
    if not isinstance(value, str) or not value:
        return value
    candidates = []
    fixed = value.replace("A0", "0")
    if fixed != value:
        candidates.append(fixed)
    candidates.append(value)
    for candidate in candidates:
        try:
            text = base64.b64decode(candidate + "=" * (-len(candidate) % 4)).decode("utf-8", "replace")
        except Exception:
            continue
        if re.match(r"^https?://", text.strip()):
            return text.strip()
    return value


# ---------------------------------------------------------------------------
# 最小 protobuf wire 解码器
# ---------------------------------------------------------------------------

def read_varint(buf, pos):
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def read_len_delimited(buf, pos):
    length, pos = read_varint(buf, pos)
    end = pos + length
    if end > len(buf):
        raise ValueError("truncated length-delimited field")
    return buf[pos:end], end


def decode(buf):
    """把 protobuf 字节流解成 {字段号: [(wire_type, 值), ...]}。"""
    fields = {}
    pos = 0
    while pos < len(buf):
        key, pos = read_varint(buf, pos)
        field_num = key >> 3
        wire_type = key & 7
        if wire_type == 0:
            value, pos = read_varint(buf, pos)
        elif wire_type == 1:
            value = buf[pos:pos + 8]
            pos += 8
        elif wire_type == 2:
            value, pos = read_len_delimited(buf, pos)
        elif wire_type == 5:
            value = buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError(f"不支持的 wire type: {wire_type}")
        fields.setdefault(field_num, []).append((wire_type, value))
    return fields


def flex(fields, num, default=None):
    """按 wire type 自动把字段转成 int/float/str。"""
    entries = fields.get(num)
    if not entries:
        return default
    wire_type, value = entries[0]
    if wire_type == 0:
        return value
    if wire_type == 1:
        return struct.unpack("<d", value)[0]
    if wire_type == 5:
        return struct.unpack("<f", value)[0]
    if wire_type == 2:
        return value.decode("utf-8", "replace")
    return default


def flex_bool(fields, num, default=None):
    entries = fields.get(num)
    if not entries:
        return default
    return bool(entries[0][1])


# ---------------------------------------------------------------------------
# 消息解析(字段号源自 AniCh 的 protobuf 定义)
# ---------------------------------------------------------------------------

VOD_ITEM_KEYS = ("url", "sort", "type", "caption", "title")
LIST_ITEM_KEYS = ("id", "title", "episode", "episodes_total", "status", "date", "image", "tagline")
EP_KEYS = ("status", "sort", "airdate", "duration", "sites", "rating", "image", "title", "overview")


def parse_vod_item(buf):
    # 1=url(混淆base64) 2=sort(uint32) 4=caption(str) 5=type(str) 8=title(str)
    fields = decode(buf)
    return {
        "url": deobfuscate_url(flex(fields, 1)),
        "sort": flex(fields, 2),
        "type": flex(fields, 5),
        "caption": flex(fields, 4),
        "title": flex(fields, 8),
    }


def parse_list_item(buf):
    # 1=id 2=title 3=episode 4=episodes_total 5=status 6=date(double) 7=image 8=tagline
    fields = decode(buf)
    return {
        "id": flex(fields, 1),
        "title": flex(fields, 2),
        "episode": flex(fields, 3),
        "episodes_total": flex(fields, 4),
        "status": flex(fields, 5),
        "date": flex(fields, 6),
        "image": flex(fields, 7),
        "tagline": flex(fields, 8),
    }


def parse_site(buf):
    # 1=site 2=id
    fields = decode(buf)
    return {"site": flex(fields, 1), "id": flex(fields, 2)}


def parse_episode(buf):
    # 1=status(bool) 2=sort 3=airdate(double) 4=duration
    # 5=sites(repeated) 6=rating 7=image 8=title 9=overview
    fields = decode(buf)
    return {
        "status": flex_bool(fields, 1),
        "sort": flex(fields, 2),
        "airdate": flex(fields, 3),
        "duration": flex(fields, 4),
        "sites": [parse_site(v) for wt, v in fields.get(5, []) if wt == 2],
        "rating": flex(fields, 6),
        "image": flex(fields, 7),
        "title": flex(fields, 8),
        "overview": flex(fields, 9),
    }


def extract_items(fields, parser, known_keys):
    """从形如 message X { repeated T data = 1; } 的消息里提取条目列表。

    优先解析字段 1;若解析不到,宽容地扫描其余所有嵌套字段(兼容字段号差异)。
    """
    def extract(num):
        out = []
        for wire_type, value in fields.get(num, []):
            if wire_type != 2:
                continue
            try:
                item = parser(value)
            except Exception:
                continue
            if item and any(item.get(k) is not None for k in known_keys):
                out.append(item)
        return out

    items = extract(1)
    if not items:
        for num in sorted(fields):
            if num != 1:
                items.extend(extract(num))
    return items


def parse_vod(buf):
    if buf[:1] == b"[":
        try:
            arr = json.loads(buf.decode("utf-8", "replace"))
            buf = bytes(int(x) & 0xFF for x in arr)
        except Exception:
            pass
    return extract_items(decode(buf), parse_vod_item, VOD_ITEM_KEYS)


def parse_bangumi_list(buf):
    # 1=data(repeated) 2=prev 3=next
    fields = decode(buf)
    items = extract_items(fields, parse_list_item, LIST_ITEM_KEYS)
    return items, flex(fields, 2), flex(fields, 3)


def parse_bangumi_episodes(buf):
    # 1=data(repeated)
    return extract_items(decode(buf), parse_episode, EP_KEYS)


def dump_proto(buf, indent=0, depth=0, out=None, max_items=12):
    """递归 dump protobuf 结构(供 probe 调试新节点)。"""
    out = out if out is not None else []
    pad = "  " * indent
    if depth > 4:
        out.append(f"{pad}...(嵌套过深,省略)")
        return out
    try:
        fields = decode(buf)
    except Exception as exc:
        out.append(f"{pad}decode 失败: {exc}; 前 64 字节 hex: {buf[:64].hex()}")
        return out
    if not fields:
        out.append(f"{pad}(空消息, {len(buf)} 字节: {buf[:64].hex()})")
        return out
    for num in sorted(fields):
        entries = fields[num]
        for wire_type, value in entries[:max_items]:
            if wire_type == 0:
                out.append(f"{pad}field {num}: varint = {value}")
            elif wire_type == 1:
                out.append(f"{pad}field {num}: double = {struct.unpack('<d', value)[0]}")
            elif wire_type == 5:
                out.append(f"{pad}field {num}: float = {struct.unpack('<f', value)[0]}")
            elif wire_type == 2:
                text = value.decode("utf-8", "replace")
                printable = "\ufffd" not in text and text.strip() and not any(ch in text for ch in "\x00\x01\x02\x03")
                if printable:
                    shown = text if len(text) <= 200 else text[:200] + "..."
                    out.append(f"{pad}field {num}: str = {shown!r}")
                else:
                    out.append(f"{pad}field {num}: bytes ({len(value)} B), 尝试嵌套解析:")
                    dump_proto(value, indent + 2, depth + 1, out, max_items)
        if len(entries) > max_items:
            out.append(f"{pad}field {num}: ... 共 {len(entries)} 条,省略其余")
    return out


# ---------------------------------------------------------------------------
# 网络请求
# ---------------------------------------------------------------------------

def _fetch_once(url, headers, timeout):
    """先 urllib 请求,非 200 或异常时自动转系统 curl.exe 兜底(规避部分 WAF/TLS 指纹拦截)。
    返回 (http_code, body_bytes)。"""
    req = urllib.request.Request(url, headers=headers)
    urllib_result = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            if resp.status == 200 and body:
                return resp.status, body
            urllib_result = (resp.status, body)
    except urllib.error.HTTPError as exc:
        urllib_result = (exc.code, exc.read())
    except Exception:
        pass
    curl = shutil.which("curl")
    if curl:
        try:
            proc = subprocess.run(
                [curl, "-sS", "-L", "--connect-timeout", str(timeout),
                 "--max-time", str(timeout), "-w", "\\n%{http_code}",
                 "-A", headers.get("User-Agent", ""), url],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=timeout * 2)
            if proc.returncode == 0:
                body, _, tail = proc.stdout.rpartition(b"\\n")
                try:
                    code = int(tail.strip())
                except ValueError:
                    code = None
                if code is not None:
                    return code, body
        except Exception:
            pass
    return urllib_result or (None, b"")


def api_get(path, timeout=TIMEOUT):
    """节点 x UA 变体轮询;全部节点都 404 才判定 ID/集数不存在。"""
    variants = [
        {"User-Agent": USER_AGENT, "Accept": "*/*"},
        {"User-Agent": BROWSER_UA, "Accept": "*/*",
         "Referer": "https://www.bilibili.com/", "Origin": "https://www.bilibili.com"},
    ]
    saw_404 = 0
    saw_http = 0
    last_error = None
    print("正在请求 API(节点 x UA 轮询,请稍候)...", file=sys.stderr, flush=True)
    for base in BASE_URLS:
        url = base.rstrip("/") + path
        for headers in variants:
            print(f"  尝试 {url}", file=sys.stderr, flush=True)
            code, body = _fetch_once(url, dict(headers), timeout)
            if code == 200 and body:
                print(f"    成功: {base}", file=sys.stderr)
                return body
            if code is not None:
                print(f"    -> HTTP {code}", file=sys.stderr)
                saw_http += 1
                if code == 404:
                    saw_404 += 1
                else:
                    last_error = f"{base}: HTTP {code}"
    if saw_http and saw_404 == saw_http:
        raise RuntimeError(f"接口返回 404,ID/集数可能不存在: {BASE_URLS[0]}{path}")
    if last_error:
        raise RuntimeError(f"所有 API 节点请求失败(最后错误: {last_error})")
    raise RuntimeError("所有 API 节点均无响应(可能被网络/WAF 拦截)。")


def fmt_date(value):
    if not isinstance(value, (int, float)):
        return str(value) if value is not None else ""
    if value > 1e11:
        value = value / 1000.0
    try:
        return datetime.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def cmd_probe(args):
    """调试命令:请求新节点并保存/dump 原始响应,用于确认 protobuf 结构。"""
    if args.url:
        url = args.url
    elif args.file:
        with open(args.file, "rb") as fh:
            body = fh.read()
        print(f"已读取 {args.file} ({len(body)} 字节)")
        for line in dump_proto(body):
            print(line)
        return
    else:
        query = urllib.parse.urlencode({"keyword": args.keyword, "skip": args.skip})
        url = f"https://anich.sends.eu.org/bangumi/search?{query}"
    variants = [
        {"User-Agent": USER_AGENT, "Accept": "*/*"},
        {"User-Agent": BROWSER_UA, "Accept": "*/*",
         "Referer": "https://anich.emmmm.eu.org/",
         "Origin": "https://anich.emmmm.eu.org"},
    ]
    for headers in variants:
        print(f"请求 {url}", file=sys.stderr, flush=True)
        code, body = _fetch_once(url, dict(headers), TIMEOUT)
        print(f"  -> HTTP {code}, {len(body)} 字节", file=sys.stderr, flush=True)
        if code == 200 and body:
            with open("raw.bin", "wb") as fh:
                fh.write(body)
            print(f"原始响应已保存到 raw.bin ({len(body)} 字节)")
            print(f"前 96 字节 hex: {body[:96].hex()}")
            if body.lstrip()[:1] in (b"{", b"["):
                print("响应是 JSON,内容如下(最多 6000 字符):")
                print(json.dumps(json.loads(body.decode("utf-8", "replace")), ensure_ascii=False, indent=2)[:6000])
            else:
                print("protobuf 结构解析:")
            for line in dump_proto(body):
                print(line)
            return
    raise RuntimeError("probe 失败:所有 UA 变体均未拿到 200 响应。")


# ---------------------------------------------------------------------------
# 子命令:search / episodes / play
# ---------------------------------------------------------------------------

def cmd_search(args):
    query = urllib.parse.urlencode({"keyword": args.keyword, "skip": args.skip})
    items, prev, nxt = parse_bangumi_list(api_get("/bangumi/search?" + query))
    if not items:
        print("未找到相关番剧。")
        return
    if args.json:
        print(json.dumps({"items": items, "prev": prev, "next": nxt}, ensure_ascii=False, indent=2))
        return
    print(f"共找到 {len(items)} 条(prev={prev}, next={nxt}):")
    for item in items:
        print(f"  ID: {item.get('id')}  标题: {item.get('title') or ''}  "
              f"集数: {item.get('episode')}/{item.get('episodes_total')}  "
              f"状态: {item.get('status')}  日期: {fmt_date(item.get('date'))}")


def cmd_episodes(args):
    items = parse_bangumi_episodes(api_get(f"/bangumi/episodes/{args.id}"))
    if not items:
        print("未获取到剧集列表。")
        return
    items.sort(key=lambda x: (x.get("sort") is None, x.get("sort") or 0))
    if args.json:
        print(json.dumps({"items": items}, ensure_ascii=False, indent=2))
        return
    print(f"共 {len(items)} 集:")
    for item in items:
        sites = ", ".join(f"{s.get('site')}:{s.get('id')}" for s in item.get("sites") or []) or "-"
        print(f"  第 {item.get('sort')} 集  {item.get('title') or ''}  "
              f"时长: {item.get('duration')}  播出: {fmt_date(item.get('airdate'))}  源: {sites}")


def load_vod_items(bangumi_id, episode):
    items = parse_vod(api_get(f"/vod/{bangumi_id}/{episode}"))
    if not items:
        raise RuntimeError("该集没有可用的线路(集数可能不存在,用 episodes 命令确认)。")
    items.sort(key=lambda x: (x.get("sort") is None, x.get("sort") or 0))
    return items


def cmd_play(args):
    items = load_vod_items(args.id, args.episode)
    if args.json:
        print(json.dumps({"items": items}, ensure_ascii=False, indent=2))
        return
    print(f"线路(共 {len(items)} 条,按 sort 排序):")
    for index, item in enumerate(items, 1):
        print(f"  [{index}] caption={item.get('caption')}  type={item.get('type')}  sort={item.get('sort')}  title={item.get('title') or '-'}")
        print(f"      {item.get('url')}")


# ---------------------------------------------------------------------------
# 子命令:download
# ---------------------------------------------------------------------------

def pick_name(bangumi_id, episode):
    target = to_int(episode)
    try:
        episodes = parse_bangumi_episodes(api_get(f"/bangumi/episodes/{bangumi_id}"))
        for item in episodes:
            if target is not None and to_int(item.get("sort")) == target:
                title = item.get("title")
                if title:
                    return re.sub(r'[\\/:*?"<>|\r\n]+', "_", str(title)).strip()
    except Exception:
        pass
    ep_s = f"E{target:02d}" if target is not None else f"E{episode}"
    return f"{bangumi_id}_{ep_s}"


def download_direct(url, out_path, referer):
    headers = {"User-Agent": BROWSER_UA}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    print("直链下载中...")
    with urllib.request.urlopen(req, timeout=60) as resp:
        ctype = resp.headers.get("Content-Type", "")
        if ctype.startswith("text/") or ctype.startswith("application/json"):
            raise RuntimeError(f"该线路返回了 {ctype},不是视频直链,请用 play 换一条线路。")
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(out_path, "wb") as fh:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  已下载 {done / 1048576:.1f} / {total / 1048576:.1f} MB "
                          f"({done * 100 // total}%)", end="", file=sys.stderr, flush=True)
        print(file=sys.stderr)


def download_hls(url, out_dir, name, referer):
    n_m3u8dl = shutil.which("N_m3u8DL-RE")
    if n_m3u8dl:
        cmd = [n_m3u8dl, url, "--save-dir", out_dir, "--save-name", name,
               "--auto-select", "--header", f"User-Agent: {BROWSER_UA}"]
        if referer:
            cmd += ["--header", f"Referer: {referer}"]
        print("使用 N_m3u8DL-RE 下载...")
        rc = subprocess.run(cmd)
        if rc.returncode != 0:
            raise RuntimeError(f"N_m3u8DL-RE 下载失败(退出码 {rc.returncode})。")
        return

    yt_dlp = shutil.which("yt-dlp")
    if yt_dlp:
        cmd = [yt_dlp, "-o", os.path.join(out_dir, name + ".%(ext)s"),
               "--add-header", f"User-Agent: {BROWSER_UA}"]
        if referer:
            cmd += ["--referer", referer]
        cmd.append(url)
        print("使用 yt-dlp 下载...")
        rc = subprocess.run(cmd)
        if rc.returncode != 0:
            raise RuntimeError(f"yt-dlp 下载失败(退出码 {rc.returncode})。")
        return

    raise RuntimeError("未找到 N_m3u8DL-RE 或 yt-dlp,无法下载 m3u8。请先安装其一(见 README.md)。")


def cmd_download(args):
    items = load_vod_items(args.id, args.episode)
    if args.line < 1 or args.line > len(items):
        raise RuntimeError(f"线路序号 {args.line} 超出范围(共 {len(items)} 条,用 play 查看)。")
    item = items[args.line - 1]
    url = item.get("url")
    if not url:
        raise RuntimeError("所选线路没有 URL。")
    if not re.match(r"^https?://", url):
        raise RuntimeError(f"该线路不是 http(s) 直链({url}),无法直接下载,请用 play 换一条线路。")

    name = args.name or pick_name(args.id, args.episode)
    out_dir = args.dir or "."
    os.makedirs(out_dir, exist_ok=True)
    print(f"线路 {args.line}: caption={item.get('caption')}  type={item.get('type')}")
    print(f"URL: {url}")
    if ".m3u8" in url:
        download_hls(url, out_dir, name, args.referer)
    else:
        download_direct(url, os.path.join(out_dir, name + ".mp4"), args.referer)
    print(f"完成,输出目录: {os.path.abspath(out_dir)}")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="anich_dl.py",
        description="AniCh 番剧资源抓取工具:搜索番剧、列出剧集、查看/下载视频线路。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_probe = sub.add_parser("probe", help="调试:请求新节点并 dump 原始 protobuf 响应")
    p_probe.add_argument("keyword", nargs="?", help="搜索关键词")
    p_probe.add_argument("--skip", type=int, default=0)
    p_probe.add_argument("--url", help="直接 dump 指定 URL")
    p_probe.add_argument("--file", help="直接 dump 本地文件")
    p_probe.set_defaults(func=cmd_probe)

    p_search = sub.add_parser("search", help="搜索番剧")
    p_search.add_argument("keyword", help="搜索关键词")
    p_search.add_argument("--skip", type=int, default=0, help="跳过条数(分页)")
    p_search.add_argument("--json", action="store_true", help="输出 JSON")
    p_search.set_defaults(func=cmd_search)

    p_ep = sub.add_parser("episodes", help="列出某番剧的全部剧集")
    p_ep.add_argument("id", help="番剧 ID(来自 search)")
    p_ep.add_argument("--json", action="store_true", help="输出 JSON")
    p_ep.set_defaults(func=cmd_episodes)

    p_play = sub.add_parser("play", help="查看某集的全部视频线路")
    p_play.add_argument("id", help="番剧 ID")
    p_play.add_argument("episode", help="集数(sort 值,来自 episodes)")
    p_play.set_defaults(func=cmd_play)
    p_play.add_argument("--json", action="store_true", help="输出 JSON")

    p_dl = sub.add_parser("download", help="下载某集的某条线路")
    p_dl.add_argument("id", help="番剧 ID")
    p_dl.add_argument("episode", help="集数(sort 值)")
    p_dl.add_argument("--line", type=int, default=1, help="线路序号(来自 play,默认 1)")
    p_dl.add_argument("--dir", help="输出目录(默认当前目录)")
    p_dl.add_argument("--name", help="输出文件名(默认用剧集标题)")
    p_dl.add_argument("--referer", help="下载时附加的 Referer(部分 m3u8 需要)")
    return parser


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    try:
        if args.command == "probe":
            cmd_probe(args)
        elif args.command == "search":
            cmd_search(args)
        elif args.command == "episodes":
            cmd_episodes(args)
        elif args.command == "play":
            cmd_play(args)
        elif args.command == "download":
            cmd_download(args)
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
