import type { CdpPage } from "./browser.js";

export interface Action {
  type: "click" | "hover" | "type" | "scroll" | "wait" | "navigate" | "drag";
  selector?: string;
  x?: number;
  y?: number;
  toX?: number;
  toY?: number;
  toSelector?: string;
  steps?: number;
  text?: string;
  url?: string;
  delay?: number;
  direction?: "up" | "down" | "left" | "right";
  amount?: number;
}

interface Frame {
  timestamp: number;
  buffer: Buffer;
  actionIndex: number;
  label: string;
}

interface InterestingFrame {
  timestamp: number;
  buffer: Buffer;
  reason: string;
  actionIndex: number;
}

export interface InteractResult {
  frames: InterestingFrame[];
  actions_completed: number;
  duration_ms: number;
  total_frames_captured: number;
}

function buffersSignificantlyDifferent(a: Buffer, b: Buffer): boolean {
  if (Math.abs(a.length - b.length) > 500) return true;
  const sampleSize = Math.min(a.length, b.length, 10000);
  const step = Math.max(1, Math.floor(Math.min(a.length, b.length) / sampleSize));
  let diffPixels = 0;
  for (let i = 0; i < Math.min(a.length, b.length); i += step) {
    if (a[i] !== b[i]) diffPixels++;
  }
  return diffPixels / sampleSize > 0.02;
}

async function executeAction(page: CdpPage, action: Action): Promise<string> {
  switch (action.type) {
    case "click": {
      if (action.x !== undefined && action.y !== undefined) {
        await page.click(action.x, action.y);
        return `clicked (${action.x}, ${action.y})`;
      }
      if (!action.selector) throw new Error("click needs selector or x/y");
      const box = await page.boundingBox(action.selector);
      if (!box) throw new Error(`Not visible: ${action.selector}`);
      await page.click(Math.round(box.x + box.width / 2), Math.round(box.y + box.height / 2));
      return `clicked ${action.selector}`;
    }
    case "hover": {
      if (action.x !== undefined && action.y !== undefined) {
        await page.mouseMove(action.x, action.y);
        return `hovered (${action.x}, ${action.y})`;
      }
      if (!action.selector) throw new Error("hover needs selector or x/y");
      const box = await page.boundingBox(action.selector);
      if (!box) throw new Error(`Not visible: ${action.selector}`);
      await page.mouseMove(Math.round(box.x + box.width / 2), Math.round(box.y + box.height / 2));
      return `hovered ${action.selector}`;
    }
    case "type": {
      if (!action.text) throw new Error("type needs text");
      if (action.selector) {
        const box = await page.boundingBox(action.selector);
        if (box) await page.click(Math.round(box.x + box.width / 2), Math.round(box.y + box.height / 2));
        await page.keyPress("Meta+a");
        await page.keyPress("Backspace");
      }
      await page.typeText(action.text, 50);
      return `typed "${action.text}"`;
    }
    case "scroll": {
      const dir = action.direction || "down";
      const amt = action.amount || 300;
      const deltaX = dir === "left" ? -amt : dir === "right" ? amt : 0;
      const deltaY = dir === "up" ? -amt : dir === "down" ? amt : 0;
      await page.wheel(deltaX, deltaY);
      return `scrolled ${dir} ${amt}px`;
    }
    case "wait": {
      const ms = action.delay || 1000;
      await new Promise((r) => setTimeout(r, ms));
      return `waited ${ms}ms`;
    }
    case "navigate": {
      if (!action.url) throw new Error("navigate needs url");
      await page.goto(action.url, { timeout: 30000 });
      return `navigated to ${action.url}`;
    }
    case "drag": {
      let fromX: number, fromY: number;
      if (action.x !== undefined && action.y !== undefined) {
        fromX = action.x;
        fromY = action.y;
      } else if (action.selector) {
        const box = await page.boundingBox(action.selector);
        if (!box) throw new Error(`Not visible: ${action.selector}`);
        fromX = Math.round(box.x + box.width / 2);
        fromY = Math.round(box.y + box.height / 2);
      } else {
        throw new Error("drag needs source: selector or x/y");
      }

      let destX: number, destY: number;
      if (action.toX !== undefined && action.toY !== undefined) {
        destX = action.toX;
        destY = action.toY;
      } else if (action.toSelector) {
        const box = await page.boundingBox(action.toSelector);
        if (!box) throw new Error(`Not visible: ${action.toSelector}`);
        destX = Math.round(box.x + box.width / 2);
        destY = Math.round(box.y + box.height / 2);
      } else {
        throw new Error("drag needs destination: toSelector or toX/toY");
      }

      const steps = action.steps || 20;
      await page.drag(fromX, fromY, destX, destY, steps);
      return `dragged (${fromX},${fromY}) → (${destX},${destY})`;
    }
    default:
      throw new Error(`Unknown action type: ${(action as any).type}`);
  }
}

export async function interact(page: CdpPage, actions: Action[], intervalMs = 100, maxFrames = 3, screenshotOpts: any = {}): Promise<InteractResult> {
  const textOnly = maxFrames === 0;
  const frames: Frame[] = [];
  let currentActionIndex = 0;
  let currentLabel = "before";
  let capturing = !textOnly;

  const captureLoop = async () => {
    while (capturing) {
      try {
        const buffer = await page.screenshot(screenshotOpts);
        frames.push({
          timestamp: Date.now(),
          buffer,
          actionIndex: currentActionIndex,
          label: currentLabel,
        });
      } catch {}
      await new Promise((r) => setTimeout(r, intervalMs));
    }
  };

  const startTime = Date.now();
  const capturePromise = textOnly ? Promise.resolve() : captureLoop();

  for (let i = 0; i < actions.length; i++) {
    currentActionIndex = i;
    currentLabel = `action[${i}]: ${actions[i].type}`;
    try {
      const result = await executeAction(page, actions[i]);
      currentLabel = result;
    } catch (e) {
      currentLabel = `error: ${e instanceof Error ? e.message : String(e)}`;
    }
    if (actions[i].delay && actions[i].type !== "wait") {
      await new Promise((r) => setTimeout(r, actions[i].delay!));
    }
  }

  if (!textOnly) {
    await new Promise((r) => setTimeout(r, intervalMs * 3));
    capturing = false;
    await capturePromise;
  }

  const duration = Date.now() - startTime;
  const interesting = textOnly ? [] : selectInterestingFrames(frames, maxFrames);

  return {
    frames: interesting,
    actions_completed: actions.length,
    duration_ms: duration,
    total_frames_captured: frames.length,
  };
}

function selectInterestingFrames(frames: Frame[], maxFrames = 3): InterestingFrame[] {
  if (frames.length === 0) return [];
  if (frames.length === 1) {
    return [{ timestamp: frames[0].timestamp, buffer: frames[0].buffer, reason: "only frame", actionIndex: frames[0].actionIndex }];
  }

  const selected: InterestingFrame[] = [];

  selected.push({ timestamp: frames[0].timestamp, buffer: frames[0].buffer, reason: "initial state", actionIndex: frames[0].actionIndex });

  let lastSelectedIdx = 0;

  for (let i = 1; i < frames.length; i++) {
    const prev = frames[i - 1];
    const curr = frames[i];
    const actionChanged = curr.actionIndex !== prev.actionIndex;
    const visualChange = buffersSignificantlyDifferent(prev.buffer, curr.buffer);

    if (actionChanged && visualChange) {
      selected.push({ timestamp: curr.timestamp, buffer: curr.buffer, reason: `state change after: ${curr.label}`, actionIndex: curr.actionIndex });
      lastSelectedIdx = i;
    } else if (visualChange && i - lastSelectedIdx >= 3) {
      selected.push({ timestamp: curr.timestamp, buffer: curr.buffer, reason: `visual update during: ${curr.label}`, actionIndex: curr.actionIndex });
      lastSelectedIdx = i;
    }
  }

  const lastFrame = frames[frames.length - 1];
  if (lastSelectedIdx !== frames.length - 1) {
    selected.push({ timestamp: lastFrame.timestamp, buffer: lastFrame.buffer, reason: "final state", actionIndex: lastFrame.actionIndex });
  }

  if (selected.length > maxFrames) {
    const keep = [selected[0]];
    const step = (selected.length - 2) / (maxFrames - 2);
    for (let i = 1; i < maxFrames - 1; i++) {
      keep.push(selected[Math.round(i * step)]);
    }
    keep.push(selected[selected.length - 1]);
    return keep;
  }

  return selected;
}
