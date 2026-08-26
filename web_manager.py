# -*- coding: utf-8 -*-
"""
本地管理页面 —— 双击「启动管理页面.bat」运行，浏览器自动打开。
功能：增删监控博主、编辑关键词，点保存后自动写入 config.yaml 并同步到 GitHub。
零第三方依赖（纯 Python 标准库），不联网也能用（只是不同步）。
"""
import json
import os
import re
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
PORT = 8787

# ------------------------------------------------------------
# config.yaml 模板：保存时重新渲染，注释得以完整保留
# ------------------------------------------------------------

CONFIG_TEMPLATE = """# ============================================================
#  财经信息监控系统 - 配置文件
#  日常修改建议用「启动管理页面.bat」，本文件由程序自动维护
# ============================================================

# ---------- 监控名单 ----------
# handle = X 用户名（不带@），note = 备注（会显示在推送里）
accounts:
@@ACCOUNTS@@

# ---------- 数据源 ----------
# provider 可选值：
#   mock          - 本地测试数据（不联网，用于调试流程）
#   rsshub        - 免费！自建 RSSHub（需在 Secrets 配置 RSSHUB_BASE 和 TWITTER_AUTH_TOKEN）
#   twitterapi_io - 按量付费接口（约$2-5/月，免费方案失效时切换到这里）
provider: @@PROVIDER@@

rsshub_base: "@@RSSHUB_BASE@@"          # 例: https://你的域名 ，留空则读环境变量 RSSHUB_BASE
twitterapi_io_key: ""    # 留空则读环境变量 TWITTERAPI_IO_KEY

# ---------- 推送通道 ----------
# wecom    = 企业微信群机器人（推荐，无第三方中转）
# pushplus = 微信公众号/PushPlus（备用）
push_channel: @@PUSH_CHANNEL@@

wecom_webhook: "@@WECOM_WEBHOOK@@"   # 留空则读环境变量 WECOM_WEBHOOK（云端走 GitHub Secrets）

# ---------- 关键词粗筛 ----------
# 推文里【命中任意一个】才进入下一轮 AI 精筛（英文不区分大小写）
keywords:
@@KEYWORDS@@

# 含以下词的直接跳过（噪音过滤）
exclude_keywords:
@@EXCLUDE@@

# ---------- AI 精筛 ----------
ai_enabled: true
# openai_compat = 任意OpenAI兼容接口（OpenRouter/智谱等，密钥走 Secrets 的 AI_API_KEY）
ai_provider: @@AI_PROVIDER@@
ai_base_url: "@@AI_BASE_URL@@"           # 例: https://openrouter.ai/api/v1
ai_model: @@AI_MODEL@@                   # 例: z-ai/glm-5.2:free
gemini_model: gemini-2.5-flash   # 备选通道：Google Gemini 免费额度（需另配 GEMINI_API_KEY）
min_score: 6                      # 影响评分 1-10，≥此值才推送；AI不可用时照推（标注未评级）

# ---------- 推送 ----------
max_push_per_run: 30              # 单次运行最多推送条数（防止刷屏/超免费额度）
"""


def _yaml_str(v):
    """输出为带引号的安全字符串，防注入"""
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_config(accounts, keywords, exclude, old_cfg):
    acc_lines = []
    for a in accounts:
        acc_lines.append(f"  - handle: {_yaml_str(a['handle'])}")
        if a.get("note"):
            acc_lines.append(f"    note: {_yaml_str(a['note'])}")
    kw_lines = [f"  - {_yaml_str(k)}" for k in keywords]
    ex_lines = [f"  - {_yaml_str(k)}" for k in exclude]
    out = CONFIG_TEMPLATE
    out = out.replace("@@ACCOUNTS@@", "\n".join(acc_lines) or "  # （暂无博主，点击页面上的添加按钮）")
    out = out.replace("@@PROVIDER@@", str(old_cfg.get("provider", "mock")))
    out = out.replace("@@RSSHUB_BASE@@", str(old_cfg.get("rsshub_base", "") or ""))
    out = out.replace("@@PUSH_CHANNEL@@", str(old_cfg.get("push_channel", "pushplus")))
    out = out.replace("@@WECOM_WEBHOOK@@", str(old_cfg.get("wecom_webhook", "") or ""))
    out = out.replace("@@AI_PROVIDER@@", str(old_cfg.get("ai_provider", "openai_compat")))
    out = out.replace("@@AI_BASE_URL@@", str(old_cfg.get("ai_base_url")
                                              or "https://openrouter.ai/api/v1"))
    out = out.replace("@@AI_MODEL@@", str(old_cfg.get("ai_model")
                                          or "z-ai/glm-5.2:free"))
    out = out.replace("@@KEYWORDS@@", "\n".join(kw_lines) or "  []")
    out = out.replace("@@EXCLUDE@@", "\n".join(ex_lines) or "  []")
    return out


HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


def sanitize(payload):
    """校验并清洗前端提交的数据；返回 (accounts, keywords, exclude, 错误列表)"""
    errors = []
    accounts, seen = [], set()
    for a in payload.get("accounts", []):
        h = str(a.get("handle", "")).strip().lstrip("@")
        if not h:
            continue
        if not HANDLE_RE.match(h):
            errors.append(f"用户名不合法: {h}（X用户名只含字母数字下划线）")
            continue
        if h.lower() in seen:
            continue
        seen.add(h.lower())
        note = str(a.get("note", "")).strip()[:50]
        accounts.append({"handle": h, "note": note})
    if not accounts:
        errors.append("至少需要保留一个博主")

    def clean_words(raw):
        words, got = [], set()
        for w in re.split(r"[\n,，、]+", str(raw)):
            w = w.strip()
            if w and w.lower() not in got and len(w) <= 60:
                got.add(w.lower())
                words.append(w)
        return words

    return accounts, clean_words(payload.get("keywords", "")), clean_words(payload.get("exclude", "")), errors


# ------------------------------------------------------------
# Git 同步：保存后自动 commit + push 到 GitHub
# ------------------------------------------------------------

def git_sync():
    def run(*args, use_proxy=True):
        env = dict(os.environ)
        if use_proxy:  # 国内直连GitHub不稳，优先走本地代理，失败自动换直连重试
            env["HTTPS_PROXY"] = env["HTTP_PROXY"] = "http://127.0.0.1:7897"
        return subprocess.run(["git"] + list(args), cwd=BASE_DIR,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=env)
    try:
        if run("rev-parse", "--is-inside-work-tree").returncode != 0:
            return False, "已保存到本地（此文件夹还不是 Git 仓库，部署后自动具备同步功能）"
        if not run("config", "--get", "remote.origin.url").stdout.strip():
            return False, "已保存到本地（尚未关联 GitHub 远程仓库）"
        run("add", "config.yaml")
        if run("diff", "--cached", "--quiet").returncode == 0:
            return True, "内容无变化，无需同步"
        c = run("commit", "-m", "管理页面更新监控名单")
        if c.returncode != 0:
            return False, f"提交失败: {c.stderr.strip()[:200]}"
        p = run("push")
        if p.returncode != 0:  # 代理失败→直连重试
            p = run("push", use_proxy=False)
        if p.returncode != 0:
            return False, f"推送失败(网络问题，稍后重试即可): {p.stderr.strip()[:200]}"
        return True, "✅ 已保存并同步到 GitHub，约5分钟后云端生效"
    except Exception as e:
        return False, f"同步异常: {e}"


# ------------------------------------------------------------
# HTTP 服务
# ------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>财经监控 · 名单管理</title>
<style>
body{font-family:"Microsoft YaHei",sans-serif;max-width:720px;margin:24px auto;padding:0 16px;color:#222;background:#fafafa}
h2{border-left:4px solid #e67e22;padding-left:10px;font-size:18px}
table{width:100%;border-collapse:collapse;background:#fff}
td{padding:6px;border-bottom:1px solid #eee}
input[type=text]{width:95%;padding:8px;border:1px solid #ccc;border-radius:4px;box-sizing:border-box}
textarea{width:100%;height:160px;padding:8px;border:1px solid #ccc;border-radius:4px;box-sizing:border-box;font-family:inherit}
button{padding:8px 14px;border:none;border-radius:4px;background:#3498db;color:#fff;cursor:pointer}
button.del{background:#e74c3c;padding:6px 10px}
button.save{background:#27ae60;font-size:16px;padding:12px 40px;width:100%}
.tip{color:#888;font-size:13px;margin:6px 0}
#msg{margin-top:12px;padding:10px;border-radius:4px;display:none}
</style></head><body>
<h1>📡 财经信息监控系统 · 名单管理</h1>

<h2>👤 监控博主名单</h2>
<div class="tip">handle 填 X 用户名（不带@，如 elonmusk）；备注会显示在推送消息里</div>
<table id="tbl"></table>
<p><button onclick="addRow()">➕ 添加博主</button> <span class="tip">删除某行后记得点下方保存</span></p>

<h2>🔑 关键词粗筛（命中才提醒）</h2>
<div class="tip">每行一个词，中英文均可，英文不分大小写</div>
<textarea id="kw"></textarea>

<h2>🚫 噪音黑名单（含这些词直接忽略）</h2>
<textarea id="ex" style="height:90px"></textarea>

<p style="text-align:center"><button class="save" onclick="save()">💾 保存并同步</button></p>
<div id="msg"></div>

<script>
let accounts=[];
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function render(){
  const t=document.getElementById('tbl');
  t.innerHTML='<tr><th style="width:45%">X 用户名 (handle)</th><th style="width:40%">备注</th><th></th></tr>'+
   accounts.map((a,i)=>`<tr>
     <td><input type="text" value="${esc(a.handle)}" onchange="upd(${i},'handle',this.value)" placeholder="如 elonmusk"></td>
     <td><input type="text" value="${esc(a.note)}" onchange="upd(${i},'note',this.value)" placeholder="如 马斯克"></td>
     <td><button class="del" onclick="delRow(${i})">删</button></td></tr>`).join('');
}
function addRow(){accounts.push({handle:'',note:''});render()}
function delRow(i){accounts.splice(i,1);render()}
function upd(i,k,v){accounts[i][k]=v}
function show(ok,text){const m=document.getElementById('msg');m.style.display='block';
 m.style.background=ok?'#d4edda':'#f8d7da';m.textContent=text;}
async function load(){
  const d=await (await fetch('/api/config')).json();
  accounts=d.accounts;render();
  document.getElementById('kw').value=(d.keywords||[]).join('\\n');
  document.getElementById('ex').value=(d.exclude_keywords||[]).join('\\n');
}
async function save(){
  const body={accounts,
    keywords:document.getElementById('kw').value,
    exclude:document.getElementById('ex').value};
  const r=await (await fetch('/api/save',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  show(r.ok,r.message);
  if(r.ok){const d=await (await fetch('/api/config')).json();accounts=d.accounts;render();}
}
load();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/config":
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            self._send(200, json.dumps({
                "accounts": [{"handle": a.get("handle", ""), "note": a.get("note", "")}
                             for a in (cfg.get("accounts") or [])],
                "keywords": cfg.get("keywords") or [],
                "exclude_keywords": cfg.get("exclude_keywords") or [],
            }, ensure_ascii=False))
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        if self.path != "/api/save":
            return self._send(404, '{"error":"not found"}')
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            accounts, kws, excludes, errors = sanitize(payload)
            if errors:
                return self._send(200, json.dumps({"ok": False, "message": "；".join(errors)}, ensure_ascii=False))
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                old_cfg = yaml.safe_load(f) or {}
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                f.write(render_config(accounts, kws, excludes, old_cfg))
            ok, msg = git_sync()
            self._send(200, json.dumps({"ok": True, "message": f"已保存。{msg}"}, ensure_ascii=False))
        except Exception as e:
            self._send(500, json.dumps({"ok": False, "message": f"服务器错误: {e}"}, ensure_ascii=False))

    def log_message(self, fmt, *args):  # 安静模式
        pass


if __name__ == "__main__":
    url = f"http://127.0.0.1:{PORT}"
    print(f"管理页面运行中: {url}  (关闭本窗口即退出)")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
