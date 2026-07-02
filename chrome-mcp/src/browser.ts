import WebSocket from "ws";
import { execSync } from "child_process";
import { join } from "path";

const CDP_PORT = 9223;
const CDP_ENDPOINT = `http://localhost:${CDP_PORT}`;
const CHROME_BINARY = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const HOME = process.env.HOME!;
const DEBUG_DATA_DIR = join(HOME, "Chrome");

interface CdpTarget {
  id: string;
  type: string;
  url: string;
  title: string;
  webSocketDebuggerUrl: string;
}

export class CdpPage {
  private ws: WebSocket;
  private pending = new Map<number, { resolve: (v: any) => void; reject: (e: Error) => void }>();
  private seq = 0;
  private _url: string;
  private _title: string;

  constructor(ws: WebSocket, url: string, title: string) {
    this.ws = ws;
    this._url = url;
    this._title = title;
    ws.on("message", (data: Buffer) => {
      const msg = JSON.parse(data.toString());
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve } = this.pending.get(msg.id)!;
        this.pending.delete(msg.id);
        resolve(msg);
      }
    });
  }

  cdp(method: string, params: any = {}): Promise<any> {
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`CDP timeout: ${method}`));
        }
      }, 30000);
    });
  }

  async evaluate(expression: string): Promise<any> {
    const r = await this.cdp("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (r.error) throw new Error(r.error.message);
    if (r.result?.exceptionDetails) {
      const desc = r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text;
      throw new Error(desc || "Evaluation failed");
    }
    return r.result?.result?.value;
  }

  async screenshot(opts: { fullPage?: boolean; quality?: number; format?: string; clip?: any } = {}): Promise<Buffer> {
    const params: any = { format: opts.format || "png" };
    if (params.format === "jpeg" && opts.quality) params.quality = opts.quality;
    if (opts.fullPage) params.captureBeyondViewport = true;
    if (opts.clip) {
      params.clip = { ...opts.clip, scale: 1 };
    } else {
      // Force scale=1 even without clip to ensure screenshot pixels match CSS pixels
      const vp = await this.evaluate("JSON.stringify({w:document.documentElement.clientWidth,h:document.documentElement.clientHeight})");
      const { w, h } = JSON.parse(vp);
      params.clip = { x: 0, y: 0, width: w, height: h, scale: 1 };
    }
    const r = await this.cdp("Page.captureScreenshot", params);
    if (r.error) throw new Error(r.error.message);
    return Buffer.from(r.result.data, "base64");
  }

  async goto(url: string, opts: { waitUntil?: string; timeout?: number } = {}): Promise<{ status: number | null }> {
    const r = await this.cdp("Page.navigate", { url });
    if (r.error) throw new Error(r.error.message);
    const timeout = opts.timeout || 30000;
    await Promise.race([
      new Promise<void>((resolve) => {
        const handler = (data: Buffer) => {
          const msg = JSON.parse(data.toString());
          if (msg.method === "Page.loadEventFired" || msg.method === "Page.domContentEventFired") {
            this.ws.off("message", handler);
            resolve();
          }
        };
        this.ws.on("message", handler);
      }),
      new Promise<void>((_, reject) => setTimeout(() => reject(new Error("Navigation timeout")), timeout)),
    ]);
    this._url = url;
    return { status: null };
  }

  url(): string { return this._url; }
  async title(): Promise<string> {
    try { return await this.evaluate("document.title"); } catch { return this._title; }
  }

  async click(x: number, y: number): Promise<void> {
    await this.cdp("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
    await this.cdp("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
  }

  async mouseMove(x: number, y: number): Promise<void> {
    await this.cdp("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
  }

  async mouseDown(x: number, y: number): Promise<void> {
    await this.cdp("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
  }

  async mouseUp(x: number, y: number): Promise<void> {
    await this.cdp("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
  }

  async drag(fromX: number, fromY: number, toX: number, toY: number, steps = 20): Promise<void> {
    await this.mouseMove(fromX, fromY);
    await this.mouseDown(fromX, fromY);
    for (let i = 1; i <= steps; i++) {
      const p = i / steps;
      await this.mouseMove(
        Math.round(fromX + (toX - fromX) * p),
        Math.round(fromY + (toY - fromY) * p)
      );
    }
    await this.mouseUp(toX, toY);
  }

  async wheel(deltaX: number, deltaY: number, x = 0, y = 0): Promise<void> {
    await this.cdp("Input.dispatchMouseEvent", { type: "mouseWheel", x, y, deltaX, deltaY });
  }

  async typeText(text: string, delay = 50): Promise<void> {
    for (const char of text) {
      await this.cdp("Input.dispatchKeyEvent", { type: "char", text: char });
      if (delay) await new Promise((r) => setTimeout(r, delay));
    }
  }

  async keyPress(key: string): Promise<void> {
    if (key === "Meta+a") {
      await this.cdp("Input.dispatchKeyEvent", { type: "keyDown", key: "Meta", code: "MetaLeft", windowsVirtualKeyCode: 91, nativeVirtualKeyCode: 91 });
      await this.cdp("Input.dispatchKeyEvent", { type: "keyDown", key: "a", code: "KeyA", windowsVirtualKeyCode: 65, nativeVirtualKeyCode: 65, modifiers: 4 });
      await this.cdp("Input.dispatchKeyEvent", { type: "keyUp", key: "a", code: "KeyA", windowsVirtualKeyCode: 65, nativeVirtualKeyCode: 65, modifiers: 4 });
      await this.cdp("Input.dispatchKeyEvent", { type: "keyUp", key: "Meta", code: "MetaLeft", windowsVirtualKeyCode: 91, nativeVirtualKeyCode: 91 });
      return;
    }
    if (key === "Backspace") {
      await this.cdp("Input.dispatchKeyEvent", { type: "keyDown", key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8, nativeVirtualKeyCode: 8 });
      await this.cdp("Input.dispatchKeyEvent", { type: "keyUp", key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8, nativeVirtualKeyCode: 8 });
      return;
    }
    await this.cdp("Input.dispatchKeyEvent", { type: "keyDown", key, code: key });
    await this.cdp("Input.dispatchKeyEvent", { type: "keyUp", key, code: key });
  }

  async selectOption(selector: string, value: string): Promise<string[]> {
    const result = await this.evaluate(`
      (() => {
        const el = document.querySelector(${JSON.stringify(selector)});
        if (!el) throw new Error("Element not found: ${selector}");
        el.value = ${JSON.stringify(value)};
        el.dispatchEvent(new Event("change", {bubbles: true}));
        return [el.value];
      })()
    `);
    return result;
  }

  async boundingBox(selector: string): Promise<{ x: number; y: number; width: number; height: number } | null> {
    const result = await this.evaluate(`
      (() => {
        const el = document.querySelector(${JSON.stringify(selector)});
        if (!el) return null;
        const r = el.getBoundingClientRect();
        if (r.width === 0 && r.height === 0) return null;
        return { x: r.x, y: r.y, width: r.width, height: r.height };
      })()
    `);
    return result;
  }

  async querySelectorAll(selector: string, attributes: string[], limit: number): Promise<Record<string, string | null>[]> {
    return await this.evaluate(`
      (() => {
        const els = document.querySelectorAll(${JSON.stringify(selector)});
        const results = [];
        for (let i = 0; i < Math.min(els.length, ${limit}); i++) {
          const el = els[i];
          const data = {};
          for (const attr of ${JSON.stringify(attributes)}) {
            if (attr === "textContent") data[attr] = el.textContent;
            else if (attr === "innerHTML") data[attr] = el.innerHTML;
            else data[attr] = el.getAttribute(attr);
          }
          results.push(data);
        }
        return results;
      })()
    `);
  }

  async waitForSelector(selector: string, opts: { state?: string; timeout?: number } = {}): Promise<void> {
    const timeout = opts.timeout || 10000;
    const state = opts.state || "visible";
    const start = Date.now();
    while (Date.now() - start < timeout) {
      const found = await this.evaluate(`
        (() => {
          const el = document.querySelector(${JSON.stringify(selector)});
          if (!el) return ${state === "detached" || state === "hidden" ? "true" : "false"};
          if (${JSON.stringify(state)} === "hidden") return el.offsetParent === null;
          if (${JSON.stringify(state)} === "detached") return false;
          return el.offsetParent !== null || el.offsetWidth > 0;
        })()
      `);
      if (found) return;
      await new Promise((r) => setTimeout(r, 200));
    }
    throw new Error(`Timeout waiting for selector: ${selector}`);
  }

  async bringToFront(): Promise<void> {
    await this.cdp("Page.bringToFront");
  }

  async pdf(): Promise<Buffer> {
    const r = await this.cdp("Page.printToPDF", { paperWidth: 8.27, paperHeight: 11.69 });
    if (r.error) throw new Error(r.error.message);
    return Buffer.from(r.result.data, "base64");
  }

  async close(): Promise<void> {
    try { await this.cdp("Page.close"); } catch {}
    this.ws.close();
  }

  isClosed(): boolean { return this.ws.readyState !== WebSocket.OPEN; }
  disconnect(): void { this.ws.close(); }
}

export class ChromeBrowser {
  private pageConns = new Map<string, CdpPage>();

  private async isCdpAvailable(): Promise<boolean> {
    try {
      const resp = await fetch(`${CDP_ENDPOINT}/json/version`);
      return resp.ok;
    } catch {
      return false;
    }
  }

  private async restartChromeWithDebugPort(): Promise<void> {
    console.error(`[ChromeMCP] CDP not available on port ${CDP_PORT} — restarting personal Chrome`);
    try { execSync(`pkill -9 -f 'user-data-dir=${DEBUG_DATA_DIR}' 2>/dev/null || true`); } catch {}
    await new Promise((r) => setTimeout(r, 3000));
    execSync(`"${CHROME_BINARY}" --remote-debugging-port=${CDP_PORT} --user-data-dir="${DEBUG_DATA_DIR}" &>/dev/null &`);
    for (let i = 0; i < 15; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      if (await this.isCdpAvailable()) {
        console.error(`[ChromeMCP] Personal Chrome started with CDP on port ${CDP_PORT}`);
        return;
      }
    }
    throw new Error(`Chrome did not bind CDP on port ${CDP_PORT} after 15s`);
  }

  private async getTargets(): Promise<CdpTarget[]> {
    if (!(await this.isCdpAvailable())) await this.restartChromeWithDebugPort();
    const resp = await fetch(`${CDP_ENDPOINT}/json`);
    const targets: CdpTarget[] = await resp.json();
    return targets.filter((t) => t.type === "page");
  }

  private async connectToTarget(target: CdpTarget): Promise<CdpPage> {
    const existing = this.pageConns.get(target.id);
    if (existing && !existing.isClosed()) return existing;

    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise<void>((resolve, reject) => {
      ws.on("open", resolve);
      ws.on("error", reject);
    });
    const page = new CdpPage(ws, target.url, target.title);
    await page.cdp("Page.enable");
    await page.cdp("Runtime.enable");
    // Force DPR=1 so screenshot pixels == CSS pixels (no coordinate scaling needed)
    const metrics = await page.evaluate("JSON.stringify({w:window.innerWidth,h:window.innerHeight,dpr:window.devicePixelRatio})");
    const { w, h, dpr } = JSON.parse(metrics);
    if (dpr !== 1) {
      await page.cdp("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile: false });
    }
    this.pageConns.set(target.id, page);
    return page;
  }

  async listPages(): Promise<{ url: string; title: string; index: number }[]> {
    const targets = await this.getTargets();
    return targets.map((t, i) => ({ url: t.url, title: t.title, index: i }));
  }

  async getPage(index?: number): Promise<CdpPage> {
    const targets = await this.getTargets();
    if (targets.length === 0) throw new Error("No pages open");
    const i = index ?? 0;
    if (i < 0 || i >= targets.length) throw new Error(`Page index ${i} out of range (0-${targets.length - 1})`);
    const page = await this.connectToTarget(targets[i]);
    await page.bringToFront();
    return page;
  }

  async newPage(url?: string): Promise<CdpPage> {
    if (!(await this.isCdpAvailable())) await this.restartChromeWithDebugPort();
    // Use browser-level websocket to create a new target
    const versionResp = await fetch(`${CDP_ENDPOINT}/json/version`);
    const { webSocketDebuggerUrl } = await versionResp.json();
    const ws = new WebSocket(webSocketDebuggerUrl);
    await new Promise<void>((r) => ws.on("open", r));

    const result: any = await new Promise((resolve) => {
      ws.on("message", (data: Buffer) => {
        const msg = JSON.parse(data.toString());
        if (msg.id === 1) resolve(msg);
      });
      ws.send(JSON.stringify({ id: 1, method: "Target.createTarget", params: { url: url || "about:blank" } }));
    });
    ws.close();

    await new Promise((r) => setTimeout(r, 1500));
    const targets = await this.getTargets();
    const newTarget = targets.find((t) => t.id === result.result?.targetId) || targets[targets.length - 1];
    return this.connectToTarget(newTarget);
  }

  disconnect(): void {
    for (const page of this.pageConns.values()) page.disconnect();
    this.pageConns.clear();
  }
}
