export type CaptureOptions = { maxWidth?: number; type?: "image/png" | "image/jpeg"; quality?: number };

type FrameCallbackVideo = HTMLVideoElement & { requestVideoFrameCallback?: (callback: () => void) => number };

const MAX_BYTES = 5 * 1024 * 1024;

export function fitSize(width: number, height: number, maxWidth: number): { width: number; height: number } {
  if (!(width > 0) || !(height > 0)) return { width: 0, height: 0 };
  const target = Math.max(1, Math.round(maxWidth));
  if (width <= target) return { width, height };
  return { width: target, height: Math.max(1, Math.round((height * target) / width)) };
}

export function captureErrorText(error: unknown): string {
  const name = (error as { name?: unknown } | null | undefined)?.name;
  if (name === "NotAllowedError" || name === "AbortError") return "Screenshot cancelled";
  if (name === "NotSupportedError") return "Screenshots aren't supported here";
  return "Screenshot failed";
}

export async function captureTab(options: CaptureOptions = {}): Promise<string> {
  const tab = await openTabStream(options);
  try { return await tab.grab(); } finally { tab.close(); }
}

export type TabStream = { grab(): Promise<string>; close(): void; readonly live: boolean };

/** Vision mode: one share prompt, then a fresh frame per message until close(). */
export async function openTabStream(options: CaptureOptions = {}): Promise<TabStream> {
  const maxWidth = options.maxWidth ?? 1600;
  const type = options.type ?? "image/jpeg";
  const quality = options.quality ?? 0.85;
  const devices = typeof navigator === "undefined" ? undefined : navigator.mediaDevices;
  if (!devices || typeof devices.getDisplayMedia !== "function") throw namedError("NotSupportedError", "getDisplayMedia unavailable");
  const stream = await devices.getDisplayMedia({ video: { displaySurface: "browser" }, audio: false, preferCurrentTab: true, selfBrowserSurface: "include" } as any);
  const video = document.createElement("video");
  video.muted = true;
  video.playsInline = true;
  video.srcObject = stream;
  try { await readFirstFrame(video); } catch (error) { stream.getTracks().forEach((track) => track.stop()); throw error; }
  const live = () => stream.getVideoTracks().some((track) => track.readyState === "live");
  return {
    get live() { return live(); },
    async grab() {
      if (!live() || !video.videoWidth || !video.videoHeight) throw new Error("no frame");
      let size = fitSize(video.videoWidth, video.videoHeight, maxWidth);
      let dataUrl = encodeFrame(video, size, type, quality);
      for (let i = 0; i < 3 && dataUrlBytes(dataUrl) > MAX_BYTES; i++) {
        size = { width: Math.max(1, Math.round(size.width * 0.75)), height: Math.max(1, Math.round(size.height * 0.75)) };
        dataUrl = encodeFrame(video, size, type, quality);
      }
      return dataUrl;
    },
    close() { stream.getTracks().forEach((track) => track.stop()); video.srcObject = null; },
  };
}

function readFirstFrame(video: HTMLVideoElement): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error("no frame")), 5000);
    const done = () => {
      window.clearTimeout(timer);
      resolve();
    };
    video.onerror = () => {
      window.clearTimeout(timer);
      reject(new Error("video failed"));
    };
    video.onloadeddata = () => {
      const rvfc = (video as FrameCallbackVideo).requestVideoFrameCallback;
      if (typeof rvfc === "function") rvfc.call(video, done);
      else done();
    };
    video.play().catch(() => {});
  });
}

function encodeFrame(video: HTMLVideoElement, size: { width: number; height: number }, type: string, quality: number): string {
  const canvas = document.createElement("canvas");
  canvas.width = size.width;
  canvas.height = size.height;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("no canvas");
  ctx.drawImage(video, 0, 0, size.width, size.height);
  return canvas.toDataURL(type, quality);
}

function dataUrlBytes(dataUrl: string): number {
  const base64 = dataUrl.slice(dataUrl.indexOf(",") + 1);
  const padding = base64.endsWith("==") ? 2 : base64.endsWith("=") ? 1 : 0;
  return Math.floor((base64.length * 3) / 4) - padding;
}

function namedError(name: string, message: string): Error {
  const error = new Error(message);
  error.name = name;
  return error;
}
