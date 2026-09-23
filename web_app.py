#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF 转 OFD —— Web 可视化界面（生产版）
特性：拖拽/多文件上传、页码选择、在线下载、密码访问、PWA（可装到手机）。

本地运行：python3 web_app.py        （默认 0.0.0.0:8080）
生产运行：gunicorn web_app:app ...  （由 Render/start 命令调用）

环境变量：
    ACCESS_PASSWORD   访问密码；不设置则不启用登录（仅本地开发用）
    SECRET_KEY        Cookie 加密密钥（生产必填）
    PORT              监听端口（默认 8080）
    PDF2OFD_MAX_MB    单文件大小上限（默认 100）
    PDF2OFD_WORK      临时工作目录（默认 /tmp/pdf2ofd_web）
"""

import os
import time
import uuid
import threading
import traceback
from pathlib import Path
from functools import wraps

from flask import (
    Flask, request, jsonify, send_file, render_template_string, abort,
    session, redirect, url_for, Response,
)

try:
    from pdf2ofd import convert as pdf2ofd_convert
except ImportError:
    pdf2ofd_convert = None

APP_DIR = Path(__file__).resolve().parent
WORK_DIR = Path(os.environ.get("PDF2OFD_WORK", "/tmp/pdf2ofd_web"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

MAX_MB = int(os.environ.get("PDF2OFD_MAX_MB", "100"))
FILE_TTL_SEC = 60 * 60  # 转换结果保留 1 小时

ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "").strip()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "false").lower() == "true"


# ── 自动清理过期文件 ────────────────────────────────────────────────────────
def _cleanup_worker():
    while True:
        time.sleep(300)
        now = time.time()
        try:
            for d in WORK_DIR.iterdir():
                if d.is_dir() and now - d.stat().st_mtime > FILE_TTL_SEC:
                    for f in sorted(d.glob("**/*"), reverse=True):
                        try:
                            if f.is_file():
                                f.unlink()
                            elif f.is_dir():
                                f.rmdir()
                        except Exception:
                            pass
                    try:
                        d.rmdir()
                    except Exception:
                        pass
        except Exception:
            pass


threading.Thread(target=_cleanup_worker, daemon=True).start()


# ── 登录页 ──────────────────────────────────────────────────────────────────
LOGIN_HTML = r"""
<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>登录 · PDF 转 OFD</title>
<link rel="icon" href="/assets/icon-192.png">
<style>
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    background:linear-gradient(160deg,#1e293b,#0f172a 55%,#111827)}
  .box{width:100%;max-width:380px;background:#fff;border-radius:18px;padding:32px 26px;
    box-shadow:0 20px 50px rgba(0,0,0,.4);text-align:center}
  .ico{font-size:44px}
  h1{font-size:20px;margin:12px 0 4px}
  p.sub{margin:0 0 22px;color:#64748b;font-size:13px}
  input{width:100%;padding:13px 14px;border:1px solid #e2e8f0;border-radius:10px;font-size:15px;
    outline:none;margin-bottom:12px}
  input:focus{border-color:#2563eb}
  button{width:100%;background:#2563eb;color:#fff;border:0;border-radius:10px;padding:13px;
    font-size:15px;font-weight:600;cursor:pointer}
  button:hover{background:#1d4ed8}
  .err{background:#fee2e2;color:#dc2626;font-size:13px;padding:9px 12px;border-radius:8px;
    margin-bottom:12px;display:none}
</style></head><body>
  <form class="box" method="post" action="/login">
    <div class="ico">🔒</div>
    <h1>PDF 转 OFD 工具</h1>
    <p class="sub">请输入访问密码</p>
    <div class="err" id="err">密码不正确，请重试</div>
    <input type="password" name="password" placeholder="访问密码" autofocus required>
    <button type="submit">进入</button>
  </form>
  <script>if(location.search.includes('err'))document.getElementById('err').style.display='block';</script>
</body></html>
"""


def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if ACCESS_PASSWORD and not session.get("authed"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapped


# ── 主页面 ──────────────────────────────────────────────────────────────────
PAGE_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>PDF 转 OFD · 在线转换</title>
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/assets/icon-192.png">
<meta name="theme-color" content="#0f172a">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="PDF转OFD">
<link rel="apple-touch-icon" href="/assets/apple-touch-icon.png">
<style>
  :root{
    --brand:#2563eb; --brand2:#1d4ed8; --ok:#16a34a; --fail:#dc2626;
    --wait:#64748b; --line:#e2e8f0; --soft:#f1f5f9; --text:#0f172a; --muted:#64748b;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
       background:linear-gradient(160deg,#1e293b 0%,#0f172a 45%,#111827 100%);min-height:100vh;color:var(--text)}
  .wrap{max-width:820px;margin:0 auto;padding:40px 20px 80px}
  header{text-align:center;color:#fff;margin-bottom:28px}
  header h1{font-size:30px;margin:0 0 8px;font-weight:700;letter-spacing:.5px}
  header p{margin:0;color:#cbd5e1;font-size:14px}
  .badge{display:inline-block;margin-top:12px;font-size:12px;color:#dbeafe;background:rgba(37,99,235,.25);
         border:1px solid rgba(96,165,250,.4);padding:4px 12px;border-radius:999px}
  .card{background:#fff;border-radius:18px;box-shadow:0 20px 50px rgba(0,0,0,.35);padding:26px}
  #drop{border:2px dashed #cbd5e1;border-radius:14px;padding:44px 20px;text-align:center;
        background:var(--soft);transition:.18s;cursor:pointer}
  #drop.hover{border-color:var(--brand);background:#eff6ff;transform:scale(1.01)}
  #drop .ico{font-size:46px;line-height:1}
  #drop h2{margin:14px 0 6px;font-size:18px}
  #drop p{margin:0;color:var(--muted);font-size:13px}
  .opts{display:flex;gap:12px;align-items:center;margin:18px 0;flex-wrap:wrap}
  .opts input[type=text]{flex:1;min-width:180px;padding:10px 14px;border:1px solid var(--line);
        border-radius:10px;font-size:14px;outline:none}
  .opts input[type=text]:focus{border-color:var(--brand)}
  .opts label{font-size:13px;color:var(--muted)}
  button.primary{background:var(--brand);color:#fff;border:0;border-radius:10px;padding:12px 26px;
        font-size:15px;font-weight:600;cursor:pointer;transition:.15s}
  button.primary:hover{background:var(--brand2)}
  button.primary:disabled{background:#94a3b8;cursor:not-allowed}
  .row-actions{display:flex;justify-content:space-between;align-items:center;margin-top:6px}
  .hint{font-size:12px;color:var(--muted)}
  ul#list{list-style:none;margin:18px 0 0;padding:0}
  li.item{display:flex;align-items:center;gap:12px;padding:13px 14px;border:1px solid var(--line);
        border-radius:12px;margin-bottom:10px;background:#fff}
  li.item .name{flex:1;min-width:0;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .size{font-size:12px;color:var(--muted);white-space:nowrap}
  .st{font-size:12px;font-weight:600;padding:4px 10px;border-radius:999px;white-space:nowrap}
  .st.wait{background:#f1f5f9;color:var(--wait)}
  .st.run{background:#dbeafe;color:#1d4ed8}
  .st.fail{background:#fee2e2;color:var(--fail)}
  a.dl{font-size:13px;font-weight:600;color:#fff;background:var(--ok);text-decoration:none;
       padding:6px 14px;border-radius:8px;white-space:nowrap}
  a.dl:hover{background:#15803d}
  .rm{border:0;background:transparent;color:#94a3b8;cursor:pointer;font-size:18px;padding:2px 6px}
  .rm:hover{color:var(--fail)}
  footer{text-align:center;color:#94a3b8;font-size:12px;margin-top:26px;line-height:1.9}
  footer a{color:#94a3b8}
  .spin{display:inline-block;width:13px;height:13px;border:2px solid rgba(29,78,216,.25);
        border-top-color:#1d4ed8;border-radius:50%;animation:sp .7s linear infinite;vertical-align:-2px;margin-right:5px}
  @keyframes sp{to{transform:rotate(360deg)}}
  #installBar{display:none;align-items:center;gap:10px;background:#1d4ed8;color:#fff;
        padding:10px 14px;border-radius:12px;margin-bottom:16px;font-size:13px}
  #installBar button{margin-left:auto;background:#fff;color:#1d4ed8;border:0;border-radius:8px;
        padding:6px 14px;font-weight:700;font-size:13px;cursor:pointer}
  #installBar .x{background:transparent;color:#bfdbfe;font-size:18px;padding:0 4px;margin-left:0}
  #iosTip{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:50;
        align-items:flex-end}
  #iosTip .sheet{background:#fff;width:100%;border-radius:18px 18px 0 0;padding:24px 22px 34px}
  #iosTip h3{margin:0 0 10px;font-size:17px}
  #iosTip p{margin:0 0 8px;font-size:14px;color:#334155;line-height:1.7}
  #iosTip .share-ico{display:inline-block;width:18px;height:18px;vertical-align:-3px}
  #iosTip button{margin-top:14px;width:100%;background:#2563eb;color:#fff;border:0;
        padding:13px;border-radius:10px;font-size:15px;font-weight:600}
  @media (max-width:600px){
    .wrap{padding:calc(20px + env(safe-area-inset-top)) 14px calc(40px + env(safe-area-inset-bottom))}
    header h1{font-size:24px}
    header p{font-size:13px;padding:0 10px}
    .card{padding:18px;border-radius:16px}
    #drop{padding:32px 14px}
    #drop .ico{font-size:40px}
    .opts label{width:100%}
    .row-actions{flex-direction:column;align-items:stretch;gap:12px}
    button.primary{width:100%;padding:14px}
    li.item{flex-wrap:wrap;padding:12px}
    li.item .name{flex:1 1 60%}
    a.dl{padding:8px 14px;text-align:center}
    footer{font-size:11px}
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>PDF 转 OFD</h1>
    <p>拖拽上传，一键转换为国家标准版式文档（GB/T 33190-2016）</p>
    <span class="badge">矢量还原 · 支持图片 · 文件 1 小时后自动删除</span>
  </header>

  <div id="installBar">
    <span>📱 可安装到主屏幕，像 App 一样使用</span>
    <button id="installBtn">安装</button>
    <button class="x" id="installClose">×</button>
  </div>

  <div class="card">
    <div id="drop">
      <div class="ico">📄</div>
      <h2>把 PDF 拖到这里</h2>
      <p>或点击选择文件，可一次选多个（单个最大 __MAX__MB）</p>
      <input id="file" type="file" accept="application/pdf,.pdf" multiple hidden>
    </div>

    <div class="opts">
      <label>页码（可选）</label>
      <input id="pages" type="text" placeholder="留空=全部；如 0,1,2 或 0-3（从 0 开始）">
    </div>

    <div class="row-actions">
      <span class="hint" id="counter">尚未选择文件</span>
      <button class="primary" id="go" disabled>开始转换</button>
    </div>

    <ul id="list"></ul>
  </div>

  <footer>
    基于开源 pdf2ofd · 转换为版式视觉复刻，重要文件请核对效果<br>
    请勿上传涉密或敏感文件到公网地址<br>
    <a href="/logout">退出登录</a>
  </footer>
</div>

<div id="iosTip">
  <div class="sheet">
    <h3>在 iPhone 上安装</h3>
    <p>1. 点击 Safari 底部的「分享」按钮
       <svg class="share-ico" viewBox="0 0 24 24" fill="#2563eb"><path d="M12 2l4 4h-2.5v6h-3V6H8l4-4z"/><path d="M5 11v8a1 1 0 001 1h12a1 1 0 001-1v-8h-2v7H7v-7H5z"/></svg>
    </p>
    <p>2. 在弹出菜单里选择「添加到主屏幕」</p>
    <p>3. 点右上角「添加」即可，桌面会出现 App 图标</p>
    <button id="iosClose">知道了</button>
  </div>
</div>

<script>
const drop=document.getElementById('drop'), fileInput=document.getElementById('file'),
      listEl=document.getElementById('list'), goBtn=document.getElementById('go'),
      counter=document.getElementById('counter'), pagesInput=document.getElementById('pages');
let queue=[];

function fmtSize(b){if(b<1024)return b+' B';if(b<1048576)return (b/1024).toFixed(1)+' KB';return (b/1048576).toFixed(1)+' MB'}

function render(){
  listEl.innerHTML='';
  queue.forEach(f=>{
    const li=document.createElement('li'); li.className='item';
    let stHtml='';
    if(f.status==='wait') stHtml='<span class="st wait">等待</span>';
    else if(f.status==='run') stHtml='<span class="st run"><span class="spin"></span>转换中</span>';
    else if(f.status==='ok') stHtml='<a class="dl" href="/download/'+f.taskId+'">下载 OFD</a>';
    else stHtml='<span class="st fail">失败</span>';
    const name=document.createElement('span'); name.className='name'; name.textContent=f.name;
    const size=document.createElement('span'); size.className='size'; size.textContent=fmtSize(f.size);
    li.appendChild(name); li.appendChild(size);
    li.insertAdjacentHTML('beforeend',stHtml);
    if(f.status==='wait'||f.status==='fail'){
      const rm=document.createElement('button'); rm.className='rm'; rm.textContent='×';
      rm.onclick=()=>{queue=queue.filter(x=>x.id!==f.id);render();};
      li.appendChild(rm);
    }
    listEl.appendChild(li);
  });
  const n=queue.length;
  counter.textContent= n? ('已选择 '+n+' 个文件') : '尚未选择文件';
  goBtn.disabled= !n || queue.some(f=>f.status==='run');
}

function addFiles(files){
  [...files].forEach(file=>{ if(/\.pdf$/i.test(file.name)){
    queue.push({id:crypto.randomUUID(),file,name:file.name,size:file.size,status:'wait'});
  }});
  render();
}

drop.onclick=()=>fileInput.click();
fileInput.onchange=e=>addFiles(e.target.files);
['dragenter','dragover'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add('hover')}));
['dragleave','drop'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove('hover')}));
drop.addEventListener('drop',e=>addFiles(e.dataTransfer.files));

async function convertOne(f){
  f.status='run'; render();
  const fd=new FormData();
  fd.append('file',f.file);
  if(pagesInput.value.trim()) fd.append('pages',pagesInput.value.trim());
  try{
    const r=await fetch('/api/convert',{method:'POST',body:fd});
    if(r.status===401){location.href='/login';return;}
    const data=await r.json();
    if(r.ok&&data.ok){f.status='ok';f.taskId=data.taskId;}
    else{f.status='fail';alert(data.error||'转换失败');}
  }catch(err){f.status='fail';alert('网络错误，请重试');}
  render();
}

goBtn.onclick=async()=>{
  pagesInput.disabled=true;
  for(const f of queue){ if(f.status==='wait'||f.status==='fail'){ await convertOne(f);} }
  pagesInput.disabled=false;
};

render();

if('serviceWorker' in navigator){
  navigator.serviceWorker.register('/sw.js').catch(()=>{});
}

const installBar=document.getElementById('installBar'),
      installBtn=document.getElementById('installBtn'),
      installClose=document.getElementById('installClose');
let deferredPrompt=null;
function isStandalone(){ return window.matchMedia('(display-mode: standalone)').matches || navigator.standalone; }
window.addEventListener('beforeinstallprompt',e=>{
  e.preventDefault(); deferredPrompt=e;
  if(!isStandalone() && !localStorage.getItem('pwaDismissed')) installBar.style.display='flex';
});
installBtn.onclick=async()=>{
  if(deferredPrompt){ deferredPrompt.prompt(); await deferredPrompt.userChoice;
    deferredPrompt=null; installBar.style.display='none'; }
};
installClose.onclick=()=>{installBar.style.display='none';localStorage.setItem('pwaDismissed','1');};
const isIOS=/iphone|ipad|ipod/i.test(navigator.userAgent);
if(isIOS && !isStandalone() && !localStorage.getItem('iosTipDismissed')){
  const iosTip=document.getElementById('iosTip');
  setTimeout(()=>{iosTip.style.display='flex';},1500);
  document.getElementById('iosClose').onclick=()=>{
    iosTip.style.display='none';localStorage.setItem('iosTipDismissed','1');
  };
}
window.addEventListener('appinstalled',()=>{installBar.style.display='none';});
</script>
</body>
</html>
"""

SW_JS = """
const CACHE='pdf2ofd-v1';
self.addEventListener('install',e=>{
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then(c=>c.addAll(['/','/manifest.json'])).catch(()=>{}));
});
self.addEventListener('activate',e=>{e.waitUntil(self.clients.claim());});
self.addEventListener('fetch',e=>{
  const url=new URL(e.request.url);
  if(url.pathname.startsWith('/api/')||url.pathname.startsWith('/download/'))return;
  if(e.request.method!=='GET')return;
  e.respondWith(
    caches.match(e.request).then(r=>r||fetch(e.request).then(resp=>{
      const copy=resp.clone();
      caches.open(CACHE).then(c=>c.put(e.request,copy)).catch(()=>{});
      return resp;
    }).catch(()=>caches.match('/')))
  );
});
"""


# ── 路由：登录相关 ──────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if not ACCESS_PASSWORD:
        return redirect(url_for("index"))
    if request.method == "GET":
        if session.get("authed"):
            return redirect(url_for("index"))
        return render_template_string(LOGIN_HTML)
    if request.form.get("password", "") == ACCESS_PASSWORD:
        session["authed"] = True
        session.permanent = True
        nxt = request.args.get("next") or url_for("index")
        if not nxt.startswith("/"):
            nxt = url_for("index")
        return redirect(nxt)
    return redirect(url_for("login", err=1))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── 路由：主页面与受保护接口 ────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    if pdf2ofd_convert is None:
        return "<h2>服务未就绪：缺少 pdf2ofd 依赖</h2>", 500
    return render_template_string(PAGE_HTML.replace("__MAX__", str(MAX_MB)))


@app.route("/api/convert", methods=["POST"])
@login_required
def api_convert():
    if pdf2ofd_convert is None:
        return jsonify({"ok": False, "error": "服务缺少依赖"}), 500
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "未收到文件"}), 400
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"ok": False, "error": "仅支持 PDF"}), 400

    pages = (request.form.get("pages") or "all").strip() or "all"
    task_id = uuid.uuid4().hex
    tdir = WORK_DIR / task_id
    tdir.mkdir(parents=True, exist_ok=True)

    safe = Path(f.filename).name
    src = tdir / safe
    f.save(src)
    out_name = Path(safe).stem + ".ofd"
    dst = tdir / out_name

    try:
        pdf2ofd_convert(str(src), str(dst), pages=pages, verbose=False)
        if not dst.exists():
            raise RuntimeError("未生成 OFD 文件")
        (tdir / "download_name.txt").write_text(out_name, encoding="utf-8")
        try:
            src.unlink()
        except Exception:
            pass
        return jsonify({"ok": True, "task_id": task_id,
                        "name": out_name, "size": dst.stat().st_size})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/download/<task_id>")
@login_required
def download(task_id):
    if not re_safe(task_id):
        abort(404)
    tdir = WORK_DIR / task_id
    if not tdir.is_dir():
        abort(404)
    ofds = list(tdir.glob("*.ofd"))
    if not ofds:
        abort(404)
    dl_name = ofds[0].name
    name_file = tdir / "download_name.txt"
    if name_file.exists():
        dl_name = name_file.read_text(encoding="utf-8").strip() or dl_name
    return send_file(str(ofds[0]), as_attachment=True, download_name=dl_name)


def re_safe(tid):
    return len(tid) == 32 and all(c in "0123456789abcdef" for c in tid)


# ── 公共资源（PWA/图标，不涉密，无需登录）──────────────────────────────────
@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "PDF 转 OFD 在线工具",
        "short_name": "PDF转OFD",
        "description": "PDF 转国家标准 OFD 版式文档",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#0f172a",
        "theme_color": "#0f172a",
        "lang": "zh-CN",
        "icons": [
            {"src": "/assets/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/assets/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    })


@app.route("/sw.js")
def sw():
    return Response(SW_JS, mimetype="application/javascript")


@app.route("/assets/<path:fname>")
def assets(fname):
    safe = Path(fname).name
    p = APP_DIR / "assets" / safe
    if not p.is_file():
        abort(404)
    return send_file(str(p))


@app.errorhandler(413)
def too_big(e):
    return jsonify({"ok": False, "error": f"文件超过 {MAX_MB}MB 限制"}), 413


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(f">> 开发服务器: http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
