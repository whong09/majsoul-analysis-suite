#!/usr/bin/env node

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  ErrorCode,
  McpError,
} from "@modelcontextprotocol/sdk/types.js";
import { ChromeBrowser, type CdpPage } from "./browser.js";
import { interact, type Action } from "./interact.js";

const TOOLS = [
  {
    name: "chrome_navigate",
    description: "Navigate a page to a URL. Returns page title and final URL after load.",
    inputSchema: {
      type: "object" as const,
      properties: {
        url: { type: "string", description: "URL to navigate to" },
        page_index: { type: "number", description: "Page index (default: 0 = active tab)" },
      },
      required: ["url"],
    },
  },
  {
    name: "chrome_new_tab",
    description: "Open a new tab, optionally navigating to a URL.",
    inputSchema: {
      type: "object" as const,
      properties: {
        url: { type: "string", description: "URL to open (blank if omitted)" },
      },
    },
  },
  {
    name: "chrome_list_tabs",
    description: "List all open tabs with their URLs and titles.",
    inputSchema: { type: "object" as const, properties: {} },
  },
  {
    name: "chrome_close_tab",
    description: "Close a tab by index.",
    inputSchema: {
      type: "object" as const,
      properties: {
        page_index: { type: "number", description: "Tab index to close" },
      },
      required: ["page_index"],
    },
  },
  {
    name: "chrome_screenshot",
    description: "Take a screenshot of a page. By default, annotates all interactive elements with numbered labels and returns an element map. Use annotate=false for a clean screenshot.",
    inputSchema: {
      type: "object" as const,
      properties: {
        page_index: { type: "number", description: "Page index (default: 0)" },
        full_page: { type: "boolean", description: "Capture full scrollable page (default: false)" },
        selector: { type: "string", description: "CSS selector to screenshot a specific element" },
        quality: { type: "number", description: "JPEG quality 1-100. If set, returns JPEG instead of PNG (much smaller)" },
        annotate: { type: "boolean", description: "Overlay numbered labels on interactive elements (default: true)" },
      },
    },
  },
  {
    name: "chrome_get_content",
    description: "Get text content or HTML of a page or element.",
    inputSchema: {
      type: "object" as const,
      properties: {
        page_index: { type: "number", description: "Page index (default: 0)" },
        selector: { type: "string", description: "CSS selector (default: body)" },
        format: { type: "string", enum: ["text", "html"], description: "Return format (default: text)" },
      },
    },
  },
  {
    name: "chrome_click",
    description: "Click an element on the page.",
    inputSchema: {
      type: "object" as const,
      properties: {
        page_index: { type: "number", description: "Page index (default: 0)" },
        selector: { type: "string", description: "CSS selector to click" },
      },
      required: ["selector"],
    },
  },
  {
    name: "chrome_type",
    description: "Type text into an input element.",
    inputSchema: {
      type: "object" as const,
      properties: {
        page_index: { type: "number", description: "Page index (default: 0)" },
        selector: { type: "string", description: "CSS selector of the input" },
        text: { type: "string", description: "Text to type" },
        clear: { type: "boolean", description: "Clear existing value first (default: true)" },
      },
      required: ["selector", "text"],
    },
  },
  {
    name: "chrome_evaluate",
    description: "Execute JavaScript in the page context. Returns the result serialized as JSON.",
    inputSchema: {
      type: "object" as const,
      properties: {
        page_index: { type: "number", description: "Page index (default: 0)" },
        expression: { type: "string", description: "JavaScript expression to evaluate" },
      },
      required: ["expression"],
    },
  },
  {
    name: "chrome_wait_for",
    description: "Wait for a selector to appear, or for navigation/network idle.",
    inputSchema: {
      type: "object" as const,
      properties: {
        page_index: { type: "number", description: "Page index (default: 0)" },
        selector: { type: "string", description: "CSS selector to wait for" },
        state: { type: "string", enum: ["visible", "hidden", "attached", "detached"], description: "Element state to wait for (default: visible)" },
        timeout: { type: "number", description: "Timeout in ms (default: 10000)" },
      },
      required: ["selector"],
    },
  },
  {
    name: "chrome_hover",
    description: "Move the mouse to an element or coordinates. Triggers hover/tooltip effects.",
    inputSchema: {
      type: "object" as const,
      properties: {
        page_index: { type: "number", description: "Page index (default: 0)" },
        selector: { type: "string", description: "CSS selector to hover (optional if x/y given)" },
        x: { type: "number", description: "X coordinate to hover" },
        y: { type: "number", description: "Y coordinate to hover" },
      },
    },
  },
  {
    name: "chrome_select",
    description: "Select an option from a <select> element.",
    inputSchema: {
      type: "object" as const,
      properties: {
        page_index: { type: "number", description: "Page index (default: 0)" },
        selector: { type: "string", description: "CSS selector of the <select>" },
        value: { type: "string", description: "Option value to select" },
      },
      required: ["selector", "value"],
    },
  },
  {
    name: "chrome_query_selector_all",
    description: "Query all elements matching a selector and return their text/attributes.",
    inputSchema: {
      type: "object" as const,
      properties: {
        page_index: { type: "number", description: "Page index (default: 0)" },
        selector: { type: "string", description: "CSS selector" },
        attributes: { type: "array", items: { type: "string" }, description: "Attributes to extract (default: [\"textContent\"])" },
        limit: { type: "number", description: "Max elements to return (default: 50)" },
      },
      required: ["selector"],
    },
  },
  {
    name: "chrome_pdf",
    description: "Save the page as a PDF. Returns base64-encoded PDF.",
    inputSchema: {
      type: "object" as const,
      properties: {
        page_index: { type: "number", description: "Page index (default: 0)" },
      },
    },
  },
  {
    name: "chrome_interact",
    description: "Execute a batch of user-like actions (click, hover, type, scroll, wait, navigate, drag) while capturing screenshots. Returns only frames with meaningful visual changes. Use this for fluid multi-step interactions instead of individual tool calls.",
    inputSchema: {
      type: "object" as const,
      properties: {
        page_index: { type: "number", description: "Page index (default: 0)" },
        actions: {
          type: "array",
          description: "Sequence of actions to perform",
          items: {
            type: "object",
            properties: {
              type: { type: "string", enum: ["click", "hover", "type", "scroll", "wait", "navigate", "drag"], description: "Action type" },
              selector: { type: "string", description: "CSS selector (for click, hover, type, drag source)" },
              x: { type: "number", description: "X coordinate (for click, hover, drag source)" },
              y: { type: "number", description: "Y coordinate (for click, hover, drag source)" },
              toX: { type: "number", description: "Destination X coordinate (for drag)" },
              toY: { type: "number", description: "Destination Y coordinate (for drag)" },
              toSelector: { type: "string", description: "Destination CSS selector (for drag)" },
              steps: { type: "number", description: "Number of intermediate mouse move steps for drag (default: 20)" },
              text: { type: "string", description: "Text to type (for type action)" },
              url: { type: "string", description: "URL (for navigate action)" },
              delay: { type: "number", description: "Extra delay after this action in ms" },
              direction: { type: "string", enum: ["up", "down", "left", "right"], description: "Scroll direction" },
              amount: { type: "number", description: "Scroll amount in pixels (default: 300)" },
            },
            required: ["type"],
          },
        },
        interval_ms: { type: "number", description: "Screenshot interval in ms (default: 100)" },
        max_frames: { type: "number", description: "Max frames to return (default: 3). Use 0 for text-only mode (no screenshots)" },
        quality: { type: "number", description: "JPEG quality 1-100. If set, returns JPEG instead of PNG (much smaller)" },
      },
      required: ["actions"],
    },
  },
];

const ANNOTATE_SCRIPT = `(() => {
  const selectors = 'a, button, input, select, textarea, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [role="option"], [onclick], [tabindex]:not([tabindex="-1"])';
  const elements = Array.from(document.querySelectorAll(selectors));
  const vw = window.innerWidth, vh = window.innerHeight;
  const results = [];
  let id = 0;
  for (const el of elements) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw) continue;
    if (window.getComputedStyle(el).visibility === 'hidden') continue;
    if (window.getComputedStyle(el).display === 'none') continue;
    const cx = Math.round(r.x + r.width / 2);
    const cy = Math.round(r.y + r.height / 2);
    const label = el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.textContent?.trim().substring(0, 30) || '';
    const badge = document.createElement('div');
    badge.setAttribute('data-mcp-annotation', '');
    badge.textContent = String(id);
    badge.style.cssText = 'position:fixed;left:' + (r.x) + 'px;top:' + (r.y) + 'px;background:rgba(255,0,0,0.85);color:#fff;font:bold 11px monospace;padding:1px 3px;border-radius:3px;z-index:2147483647;pointer-events:none;line-height:1.2;';
    document.body.appendChild(badge);
    results.push({ id, tag: el.tagName.toLowerCase(), role: el.getAttribute('role'), label, x: cx, y: cy, width: Math.round(r.width), height: Math.round(r.height) });
    id++;
  }
  return results;
})()`;

class ChromeMcpServer {
  private server: Server;
  private browser: ChromeBrowser;

  constructor() {
    this.browser = new ChromeBrowser();
    this.server = new Server(
      { name: "chrome-mcp-server", version: "2.0.0" },
      { capabilities: { tools: {} } }
    );
    this.setupHandlers();
    this.server.onerror = (error) => console.error("[MCP Error]", error);
    process.on("SIGINT", async () => {
      await this.server.close();
      process.exit(0);
    });
  }

  private setupHandlers() {
    this.server.setRequestHandler(ListToolsRequestSchema, async () => ({
      tools: TOOLS,
    }));

    this.server.setRequestHandler(CallToolRequestSchema, async (request) => {
      const args: any = request.params.arguments || {};
      try {
        switch (request.params.name) {
          case "chrome_navigate": return await this.navigate(args);
          case "chrome_new_tab": return await this.newTab(args);
          case "chrome_list_tabs": return await this.listTabs();
          case "chrome_close_tab": return await this.closeTab(args);
          case "chrome_screenshot": return await this.screenshot(args);
          case "chrome_get_content": return await this.getContent(args);
          case "chrome_click": return await this.clickTool(args);
          case "chrome_hover": return await this.hover(args);
          case "chrome_type": return await this.typeTool(args);
          case "chrome_evaluate": return await this.evaluateTool(args);
          case "chrome_wait_for": return await this.waitFor(args);
          case "chrome_select": return await this.select(args);
          case "chrome_query_selector_all": return await this.querySelectorAll(args);
          case "chrome_pdf": return await this.pdf(args);
          case "chrome_interact": return await this.interactTool(args);
          default:
            throw new McpError(ErrorCode.MethodNotFound, `Unknown tool: ${request.params.name}`);
        }
      } catch (error) {
        const msg = error instanceof Error ? error.message : String(error);
        if (msg.includes("connect") || msg.includes("Target closed") || msg.includes("disconnected")) {
          this.browser.disconnect();
        }
        return { content: [{ type: "text" as const, text: `Error: ${msg}` }], isError: true };
      }
    });
  }

  private text(data: unknown) {
    return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
  }

  private async navigate(args: any) {
    const page = await this.browser.getPage(args.page_index);
    await page.goto(args.url, { timeout: 30000 });
    const title = await page.title();
    return this.text({ url: args.url, title });
  }

  private async newTab(args: any) {
    const page = await this.browser.newPage(args.url);
    return this.text({ url: page.url(), title: await page.title() });
  }

  private async listTabs() {
    return this.text(await this.browser.listPages());
  }

  private async closeTab(args: any) {
    const page = await this.browser.getPage(args.page_index);
    const url = page.url();
    await page.close();
    return this.text({ closed: url });
  }

  private async screenshot(args: any) {
    const page = await this.browser.getPage(args.page_index);
    const opts: any = {};
    if (args.quality) { opts.format = "jpeg"; opts.quality = args.quality; }
    if (args.full_page) opts.fullPage = true;
    if (args.selector) {
      const box = await page.boundingBox(args.selector);
      if (!box) throw new Error(`Element not found or not visible: ${args.selector}`);
      opts.clip = { x: box.x, y: box.y, width: box.width, height: box.height };
    }

    const annotate = args.annotate !== false && !args.selector;
    let elementMap: any[] | null = null;

    if (annotate) {
      elementMap = await page.evaluate(ANNOTATE_SCRIPT);
    }

    const buffer = await page.screenshot(opts);

    if (annotate) {
      await page.evaluate("document.querySelectorAll('[data-mcp-annotation]').forEach(e => e.remove())");
    }

    const mimeType = args.quality ? "image/jpeg" : "image/png";
    const content: any[] = [];
    if (elementMap && elementMap.length > 0) {
      const mapText = elementMap.map((e: any) => `[${e.id}] ${e.tag}${e.role ? `[role=${e.role}]` : ""}${e.label ? ` "${e.label}"` : ""} @ (${e.x},${e.y}) ${e.width}x${e.height}`).join("\n");
      content.push({ type: "text" as const, text: `Interactive elements:\n${mapText}` });
    }
    content.push({ type: "image" as const, data: buffer.toString("base64"), mimeType });
    return { content };
  }

  private async getContent(args: any) {
    const page = await this.browser.getPage(args.page_index);
    const selector = args.selector || "body";
    const format = args.format || "text";
    const prop = format === "html" ? "innerHTML" : "innerText";
    const content = await page.evaluate(`document.querySelector(${JSON.stringify(selector)})?.${prop} || ""`);
    const truncated = content.length > 50000 ? content.slice(0, 50000) + "\n...[truncated]" : content;
    return this.text({ selector, format, length: content.length, content: truncated });
  }

  private async clickTool(args: any) {
    const page = await this.browser.getPage(args.page_index);
    const box = await page.boundingBox(args.selector);
    if (!box) throw new Error(`Element not found or not visible: ${args.selector}`);
    const x = Math.round(box.x + box.width / 2);
    const y = Math.round(box.y + box.height / 2);
    await page.click(x, y);
    return this.text({ clicked: args.selector, at: { x, y } });
  }

  private async hover(args: any) {
    const page = await this.browser.getPage(args.page_index);
    let x: number, y: number;
    if (args.x !== undefined && args.y !== undefined) {
      x = args.x; y = args.y;
    } else if (args.selector) {
      const box = await page.boundingBox(args.selector);
      if (!box) throw new Error(`Element not found or not visible: ${args.selector}`);
      x = Math.round(box.x + box.width / 2);
      y = Math.round(box.y + box.height / 2);
    } else {
      throw new Error("Provide either a selector or x/y coordinates");
    }
    await page.mouseMove(x, y);
    return this.text({ hovered: { x, y } });
  }

  private async typeTool(args: any) {
    const page = await this.browser.getPage(args.page_index);
    if (args.selector) {
      const box = await page.boundingBox(args.selector);
      if (box) await page.click(Math.round(box.x + box.width / 2), Math.round(box.y + box.height / 2));
    }
    if (args.clear !== false) {
      await page.keyPress("Meta+a");
      await page.keyPress("Backspace");
    }
    await page.typeText(args.text, 50);
    return this.text({ typed: args.text, into: args.selector });
  }

  private async evaluateTool(args: any) {
    const page = await this.browser.getPage(args.page_index);
    const result = await page.evaluate(args.expression);
    return this.text({ result });
  }

  private async waitFor(args: any) {
    const page = await this.browser.getPage(args.page_index);
    await page.waitForSelector(args.selector, { state: args.state, timeout: args.timeout });
    return this.text({ found: args.selector, state: args.state || "visible" });
  }

  private async select(args: any) {
    const page = await this.browser.getPage(args.page_index);
    const values = await page.selectOption(args.selector, args.value);
    return this.text({ selected: values });
  }

  private async querySelectorAll(args: any) {
    const page = await this.browser.getPage(args.page_index);
    const attrs = args.attributes || ["textContent"];
    const limit = args.limit || 50;
    const results = await page.querySelectorAll(args.selector, attrs, limit);
    const total = await page.evaluate(`document.querySelectorAll(${JSON.stringify(args.selector)}).length`);
    return this.text({ selector: args.selector, total, returned: results.length, elements: results });
  }

  private async pdf(args: any) {
    const page = await this.browser.getPage(args.page_index);
    const buffer = await page.pdf();
    return { content: [{ type: "text" as const, text: `data:application/pdf;base64,${buffer.toString("base64")}` }] };
  }

  private async interactTool(args: any) {
    const page = await this.browser.getPage(args.page_index);
    const actions: Action[] = args.actions || [];
    const intervalMs = args.interval_ms || 100;
    const maxFrames = args.max_frames ?? 3;
    const screenshotOpts: any = {};
    if (args.quality) { screenshotOpts.format = "jpeg"; screenshotOpts.quality = args.quality; }

    const result = await interact(page, actions, intervalMs, maxFrames, screenshotOpts);

    const content: any[] = [];
    const mimeType = args.quality ? "image/jpeg" : "image/png";
    content.push({
      type: "text" as const,
      text: JSON.stringify({
        actions_completed: result.actions_completed,
        duration_ms: result.duration_ms,
        total_frames_captured: result.total_frames_captured,
        interesting_frames: result.frames.length,
        frame_annotations: result.frames.map((f, i) => ({
          frame: i,
          reason: f.reason,
          action_index: f.actionIndex,
          relative_ms: f.timestamp - (result.frames[0]?.timestamp || 0),
        })),
      }, null, 2),
    });

    for (const frame of result.frames) {
      content.push({
        type: "image" as const,
        data: frame.buffer.toString("base64"),
        mimeType,
      });
    }

    return { content };
  }

  async run() {
    console.error("Chrome MCP Server v2.0.0 — Raw CDP mode (connects to Chrome on port 9223)");
    const transport = new StdioServerTransport();
    await this.server.connect(transport);
    console.error("Chrome MCP server running on stdio");
  }
}

const server = new ChromeMcpServer();
server.run().catch(console.error);
