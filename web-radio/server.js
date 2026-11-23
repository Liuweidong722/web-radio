#!/usr/bin/env node
/* 网络收音机 · 在线服务（零依赖，单端口）
 *
 * 职责：
 *   1. 托管静态前端（index.html / sw.js / manifest / lib / 图标）
 *   2. 提供同源流代理 GET /proxy?url=<enc>
 *      - 浏览器端直连失败（CORS / 混合内容 / 防盗链）时回落到这里
 *      - 服务端转发不受浏览器同源策略限制；m3u8 播放列表会把里面的
 *        相对/绝对 URI 改写成指向本代理的地址，保证分片也走代理
 *      - 内置 SSRF 防护：拒绝私网/回环/链路本地地址
 *
 * 运行：PORT=<port> node server.js   （PORT 由托管平台注入）
 */
"use strict";

const http = require("http");
const https = require("https");
const fs = require("fs");
const path = require("path");
const net = require("net");
const dns = require("dns");
const { URL } = require("url");

const PORT = parseInt(process.env.PORT || "8080", 10);
const ROOT = __dirname;
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";
const MAX_REDIRECT = 3;
const CONNECT_TIMEOUT_MS = 12000;
const MAX_PROXY_ACTIVE = 48;        // 并发代理上限（防滥用）
const MAX_PLAYLIST_BYTES = 2 * 1024 * 1024;
const DENY_STATIC = ["/server.js", "/package.json"];  // 不对外暴露服务端源码

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".webmanifest": "application/manifest+json; charset=utf-8",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".woff2": "font/woff2",
  ".map": "application/json; charset=utf-8"
};

/* ---------------- SSRF 防护：拒绝私网地址 ---------------- */
function isPrivateIp(ip) {
  if (!ip) return true;
  const v = ip.toLowerCase().replace(/^::ffff:/, "");
  if (v === "::1" || v === "::" || v === "") return true;
  if (v.startsWith("fc") || v.startsWith("fd")) return true;      // IPv6 ULA
  if (/^\d+\.\d+\.\d+\.\d+$/.test(v)) {
    const p = v.split(".").map(Number);
    if (p[0] === 0 || p[0] === 10 || p[0] === 127) return true;
    if (p[0] === 172 && p[1] >= 16 && p[1] <= 31) return true;
    if (p[0] === 192 && p[1] === 168) return true;
    if (p[0] === 169 && p[1] === 254) return true;                 // 链路本地/元数据
    if (p[0] === 100 && p[1] >= 64 && p[1] <= 127) return true;    // CGNAT
    return false;
  }
  return false;
}

async function hostIsPrivate(hostname) {
  if (net.isIP(hostname)) return isPrivateIp(hostname);
  try {
    const addrs = await dns.promises.lookup(hostname, { all: true });
    if (!addrs || !addrs.length) return true;
    return addrs.every(a => isPrivateIp(a.address));
  } catch (e) {
    return true;   // 解析失败视为不安全
  }
}

/* ---------------- m3u8 URI 改写 ---------------- */
function rewritePlaylist(text, base) {
  const wrap = abs => "/proxy?url=" + encodeURIComponent(abs);
  const resolve = ref => {
    try {
      const abs = new URL(ref, base).href;
      return /^https?:/.test(abs) ? abs : null;   // data:/blob: 等不改写
    } catch (e) { return null; }
  };
  return text.split(/\r?\n/).map(line => {
    if (!line) return line;
    if (line.charAt(0) === "#") {
      // EXT-X-MEDIA / EXT-X-KEY / EXT-X-MAP / EXT-X-I-FRAME-STREAM-INF 的 URI="…"
      return line.replace(/URI="([^"]+)"/g, (m, ref) => {
        const abs = resolve(ref);
        return abs ? 'URI="' + wrap(abs) + '"' : m;
      });
    }
    const abs = resolve(line.trim());
    return abs ? wrap(abs) : line;
  }).join("\n");
}

/* ---------------- 上游抓取（支持重定向 / 播放列表改写 / 流式透传） ---------------- */
function fetchUpstream(u, hop, res) {
  const lib = u.protocol === "https:" ? https : http;
  const headers = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "close"
  };
  if (u.protocol === "https:") headers.Referer = u.origin + "/";

  const preq = lib.get(u, { headers, timeout: CONNECT_TIMEOUT_MS }, (pr) => {
    const code = pr.statusCode || 502;

    // 重定向：校验目标后跟进
    if (code >= 300 && code < 400 && pr.headers.location && hop < MAX_REDIRECT) {
      pr.resume();
      let next;
      try { next = new URL(pr.headers.location, u); } catch (e) {
        res.writeHead(502); res.end(); return;
      }
      if (next.protocol !== "http:" && next.protocol !== "https:") {
        res.writeHead(400); res.end(); return;
      }
      hostIsPrivate(next.hostname).then(bad => {
        if (bad) { res.writeHead(403); res.end("forbidden"); return; }
        fetchUpstream(next, hop + 1, res);
      });
      return;
    }

    const ctype = (pr.headers["content-type"] || "").toLowerCase();
    const isPlaylist = /\.m3u8($|\?)/i.test(u.pathname + u.search) ||
                       ctype.indexOf("mpegurl") >= 0 || ctype.indexOf("m3u8") >= 0;

    if (code === 200 && isPlaylist) { collectPlaylist(pr, u, res); return; }

    const out = {
      "Cache-Control": "no-store",
      "Access-Control-Allow-Origin": "*"
    };
    if (ctype) out["Content-Type"] = pr.headers["content-type"];
    if (pr.headers["content-length"]) out["Content-Length"] = pr.headers["content-length"];
    res.writeHead(code, out);
    pr.pipe(res);
    pr.on("error", () => { try { res.destroy(); } catch (e) {} });
    res.on("error", () => { try { pr.destroy(); } catch (e) {} });
  });

  preq.on("timeout", () => preq.destroy(new Error("upstream timeout")));
  preq.on("error", (e) => {
    if (!res.headersSent) {
      res.writeHead(502, { "Content-Type": "text/plain; charset=utf-8" });
      res.end("upstream error" + (e.code ? ": " + e.code : ""));
    } else {
      try { res.destroy(); } catch (err) {}
    }
  });
}

function collectPlaylist(pr, u, res) {
  const chunks = [];
  let size = 0, aborted = false;
  pr.on("data", c => {
    size += c.length;
    if (size > MAX_PLAYLIST_BYTES) { aborted = true; pr.destroy(); return; }
    chunks.push(c);
  });
  pr.on("end", () => {
    if (aborted) { try { res.destroy(); } catch (e) {} return; }
    let out;
    try { out = rewritePlaylist(Buffer.concat(chunks).toString("utf8"), u.href); }
    catch (e) { res.writeHead(502); res.end(); return; }
    res.writeHead(200, {
      "Content-Type": "application/vnd.apple.mpegurl; charset=utf-8",
      "Cache-Control": "no-store",
      "Access-Control-Allow-Origin": "*",
      "Content-Length": Buffer.byteLength(out)
    });
    res.end(out);
  });
  pr.on("error", () => {
    if (!res.headersSent) { res.writeHead(502); res.end(); }
    else { try { res.destroy(); } catch (e) {} }
  });
}

let proxyActive = 0;
function handleProxy(res, target) {
  if (proxyActive >= MAX_PROXY_ACTIVE) {
    res.writeHead(503, { "Content-Type": "text/plain; charset=utf-8" });
    res.end("too many proxy requests");
    return;
  }
  let u;
  try { u = new URL(target); } catch (e) {
    res.writeHead(400, { "Content-Type": "text/plain; charset=utf-8" });
    res.end("missing or invalid url parameter");
    return;
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") {
    res.writeHead(400); res.end("only http/https is allowed"); return;
  }
  hostIsPrivate(u.hostname).then(bad => {
    if (bad) { res.writeHead(403); { res.end("forbidden"); } return; }
    proxyActive++;
    let released = false;
    const release = () => { if (!released) { released = true; proxyActive--; } };
    res.on("close", release);
    res.on("finish", release);
    fetchUpstream(u, 0, res);
  });
}

/* ---------------- 静态文件 ---------------- */
function serveStatic(req, res, pathname) {
  if (DENY.indexOf(pathname) >= 0) {
    res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
    res.end("404 Not Found");
    return;
  }
  const fp = path.normalize(path.join(ROOT, pathname));
  if (fp !== ROOT && fp.indexOf(ROOT + path.sep) !== 0) {
    res.writeHead(403); res.end(); return;
  }
  fs.stat(fp, (err, st) => {
    if (err || !st.isFile()) {
      res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
      res.end("404 Not Found");
      return;
    }
    const ext = path.extname(fp).toLowerCase();
    const head = {
      "Content-Type": MIME[ext] || "application/octet-stream",
      "Cache-Control": "no-cache",
      "Content-Length": st.size,
      "X-Content-Type-Options": "nosniff"
    };
    if (req.method === "HEAD") { res.writeHead(200, head); res.end(); return; }
    res.writeHead(200, head);
    fs.createReadStream(fp).on("error", () => res.destroy()).pipe(res);
  });
}

const DENY = DENY_STATIC;

/* ---------------- 入口 ---------------- */
const server = http.createServer((req, res) => {
  let u;
  try { u = new URL(req.url, "http://localhost"); } catch (e) {
    res.writeHead(400); res.end(); return;
  }

  if (u.pathname === "/proxy") {
    if (req.method !== "GET") { res.writeHead(405); res.end(); return; }
    handleProxy(res, u.searchParams.get("url") || "");
    return;
  }

  if (req.method !== "GET" && req.method !== "HEAD") {
    res.writeHead(405); res.end(); return;
  }
  let pathname;
  try { pathname = decodeURIComponent(u.pathname); } catch (e) {
    res.writeHead(400); res.end(); return;
  }
  if (pathname === "/" || pathname === "") pathname = "/index.html";
  serveStatic(req, res, pathname);
});

server.listen(PORT, "0.0.0.0", () => {
  console.log("[web-radio] listening on 0.0.0.0:" + PORT);
});
