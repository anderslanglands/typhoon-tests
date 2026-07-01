const wasmUrl = new URL("typhoon_exr_wasm.wasm", import.meta.url);
let wasmPromise = null;
const imageCache = new Map();

function loadWasm() {
  if (!wasmPromise) {
    wasmPromise = fetch(wasmUrl)
      .then((response) => {
        if (!response.ok) throw new Error(`failed to load ${wasmUrl}: ${response.status}`);
        return response.arrayBuffer();
      })
      .then((bytes) => WebAssembly.instantiate(bytes, {}))
      .then(({ instance }) => instance.exports);
  }
  return wasmPromise;
}

async function decodeExr(src, transfer = "linear") {
  const url = new URL(src, document.baseURI).href;
  const cacheKey = `${transfer}:${url}`;
  if (imageCache.has(cacheKey)) return imageCache.get(cacheKey);

  const promise = (async () => {
    const exports = await loadWasm();
    const response = await fetch(url);
    if (!response.ok) throw new Error(`failed to load ${src}: ${response.status}`);
    const bytes = new Uint8Array(await response.arrayBuffer());
    const ptr = exports.typhoon_exr_alloc(bytes.byteLength);
    if (!ptr) throw new Error(`failed to allocate ${bytes.byteLength} bytes for ${src}`);

    try {
      new Uint8Array(exports.memory.buffer, ptr, bytes.byteLength).set(bytes);
      const ok = exports.typhoon_exr_decode(ptr, bytes.byteLength);
      if (!ok) throw new Error(readWasmError(exports));
      const width = exports.typhoon_exr_width();
      const height = exports.typhoon_exr_height();
      const pixelsPtr = exports.typhoon_exr_pixels_ptr();
      const pixelsLen = exports.typhoon_exr_pixels_len();
      const pixels = new Float32Array(exports.memory.buffer, pixelsPtr, pixelsLen).slice();
      return { src, width, height, pixels, transfer };
    } finally {
      exports.typhoon_exr_dealloc(ptr, bytes.byteLength);
    }
  })();

  imageCache.set(cacheKey, promise);
  return promise;
}

function isExrSource(src) {
  return new URL(src, document.baseURI).pathname.toLowerCase().endsWith(".exr");
}

async function loadImageSource(src, transfer = "linear") {
  if (isExrSource(src)) return decodeExr(src, transfer);
  return decodeBrowserImage(src);
}

async function decodeBrowserImage(src) {
  const url = new URL(src, document.baseURI).href;
  const cacheKey = `browser:${url}`;
  if (imageCache.has(cacheKey)) return imageCache.get(cacheKey);

  const promise = new Promise((resolve, reject) => {
    const element = new Image();
    element.decoding = "async";
    element.onload = () => {
      const canvas = document.createElement("canvas");
      canvas.width = element.naturalWidth;
      canvas.height = element.naturalHeight;
      const context = canvas.getContext("2d", { willReadFrequently: true });
      context.drawImage(element, 0, 0);
      const rgba = context.getImageData(0, 0, canvas.width, canvas.height).data;
      const pixels = new Float32Array(canvas.width * canvas.height * 3);
      for (let source = 0, dest = 0; source < rgba.length; source += 4, dest += 3) {
        pixels[dest] = rgba[source] / 255;
        pixels[dest + 1] = rgba[source + 1] / 255;
        pixels[dest + 2] = rgba[source + 2] / 255;
      }
      resolve({ src, width: canvas.width, height: canvas.height, pixels, transfer: "display" });
    };
    element.onerror = () => reject(new Error(`failed to load ${src}`));
    element.src = url;
  });

  imageCache.set(cacheKey, promise);
  return promise;
}

function readWasmError(exports) {
  const ptr = exports.typhoon_exr_error_ptr();
  const len = exports.typhoon_exr_error_len();
  if (!ptr || !len) return "EXR decode failed";
  const bytes = new Uint8Array(exports.memory.buffer, ptr, len).slice();
  return new TextDecoder().decode(bytes) || "EXR decode failed";
}

function linearToSrgb(value) {
  if (!Number.isFinite(value)) return value > 0 ? 1 : 0;
  return value <= 0.0031308 ? value * 12.92 : 1.055 * Math.pow(Math.max(value, 0.0031308), 1 / 2.4) - 0.055;
}

function toByte(value) {
  if (!Number.isFinite(value)) return value > 0 ? 255 : 0;
  return Math.max(0, Math.min(255, Math.round(Math.max(0, Math.min(1, value)) * 255)));
}

function srgbBytesFor(image, r, g, b) {
  if (!image) return ["", "", ""];
  if (image.transfer === "display") return [toByte(r), toByte(g), toByte(b)];
  return [toByte(linearToSrgb(r)), toByte(linearToSrgb(g)), toByte(linearToSrgb(b))];
}

function drawDecoded(canvas, image) {
  if (!image) return;
  canvas.width = image.width;
  canvas.height = image.height;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  const imageData = context.createImageData(image.width, image.height);
  for (let pixel = 0, byte = 0; pixel < image.width * image.height; pixel += 1, byte += 4) {
    const sample = pixel * 3;
    const rgb = srgbBytesFor(
      image,
      image.pixels[sample],
      image.pixels[sample + 1],
      image.pixels[sample + 2],
    );
    imageData.data[byte] = rgb[0];
    imageData.data[byte + 1] = rgb[1];
    imageData.data[byte + 2] = rgb[2];
    imageData.data[byte + 3] = 255;
  }
  context.putImageData(imageData, 0, 0);
}

function drawThumbnail(canvas, image, maxSize = 74) {
  if (!image) return;
  const scale = Math.min(maxSize / image.width, maxSize / image.height);
  const width = Math.max(1, Math.round(image.width * scale));
  const height = Math.max(1, Math.round(image.height * scale));
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  const imageData = context.createImageData(width, height);

  for (let y = 0; y < height; y += 1) {
    const sourceY = Math.max(0, Math.min(image.height - 1, Math.floor((y + 0.5) * image.height / height)));
    for (let x = 0; x < width; x += 1) {
      const sourceX = Math.max(0, Math.min(image.width - 1, Math.floor((x + 0.5) * image.width / width)));
      const source = (sourceY * image.width + sourceX) * 3;
      const rgb = srgbBytesFor(
        image,
        image.pixels[source],
        image.pixels[source + 1],
        image.pixels[source + 2],
      );
      const dest = (y * width + x) * 4;
      imageData.data[dest] = rgb[0];
      imageData.data[dest + 1] = rgb[1];
      imageData.data[dest + 2] = rgb[2];
      imageData.data[dest + 3] = 255;
    }
  }

  context.putImageData(imageData, 0, 0);
}

async function initializeThumbnailStrip(strip) {
  if (strip.dataset.thumbnailsInitialized === "true") return;
  strip.dataset.thumbnailsInitialized = "true";
  const status = strip.querySelector("[data-thumbnail-status]");
  try {
    const canvases = Array.from(strip.querySelectorAll("[data-thumbnail-canvas]"));
    await Promise.all(canvases.map(async (canvas) => {
      const src = canvas.dataset.thumbnailSrc;
      if (!src) return;
      const transfer = canvas.dataset.thumbnailTransfer || "linear";
      const image = await loadImageSource(src, transfer);
      drawThumbnail(canvas, image);
    }));
    if (status) status.textContent = "";
  } catch (error) {
    strip.dataset.thumbnailsInitialized = "false";
    if (status) status.textContent = String(error.message || error);
  }
}

function drawZoom(canvas, image, centerX, centerY) {
  if (!image) return;
  const width = Math.max(1, image.width);
  const height = Math.max(1, image.height);
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  const imageData = context.createImageData(width, height);
  const zoom = 16;
  const sourceWidth = Math.max(1, Math.ceil(width / zoom));
  const sourceHeight = Math.max(1, Math.ceil(height / zoom));
  const startX = Math.max(0, Math.min(image.width - sourceWidth, centerX - Math.floor(sourceWidth / 2)));
  const startY = Math.max(0, Math.min(image.height - sourceHeight, centerY - Math.floor(sourceHeight / 2)));

  for (let y = 0; y < height; y += 1) {
    const sourceY = Math.max(0, Math.min(image.height - 1, startY + Math.floor(y / zoom)));
    for (let x = 0; x < width; x += 1) {
      const sourceX = Math.max(0, Math.min(image.width - 1, startX + Math.floor(x / zoom)));
      const source = (sourceY * image.width + sourceX) * 3;
      const rgb = srgbBytesFor(
        image,
        image.pixels[source],
        image.pixels[source + 1],
        image.pixels[source + 2],
      );
      const dest = (y * width + x) * 4;
      imageData.data[dest] = rgb[0];
      imageData.data[dest + 1] = rgb[1];
      imageData.data[dest + 2] = rgb[2];
      imageData.data[dest + 3] = 255;
    }
  }

  context.putImageData(imageData, 0, 0);
  context.strokeStyle = "#ffffff";
  context.lineWidth = 1;
  context.strokeRect(
    Math.floor((centerX - startX) * zoom) + 0.5,
    Math.floor((centerY - startY) * zoom) + 0.5,
    zoom,
    zoom,
  );
}

function samplePixel(image, x, y) {
  if (!image || x < 0 || y < 0 || x >= image.width || y >= image.height) return null;
  const offset = (y * image.width + x) * 3;
  return [image.pixels[offset], image.pixels[offset + 1], image.pixels[offset + 2]];
}

function formatFloat(value) {
  if (value === null || value === undefined || value === "") return "";
  if (!Number.isFinite(value)) return String(value);
  const abs = Math.abs(value);
  return abs !== 0 && (abs < 0.001 || abs >= 10000) ? value.toExponential(5) : value.toFixed(6);
}

function formatFloatTriplet(values) {
  return values ? values.map(formatFloat).join("  ") : "";
}

function formatByteTriplet(values) {
  return values ? values.join("  ") : "";
}

function updatePixelReadout(viewer, state, x, y) {
  const coordinate = viewer.querySelector("[data-pixel-coordinate]");
  if (coordinate) coordinate.textContent = `${x}, ${y}`;

  const rows = [
    ["reference", state.reference],
    ["render", state.render],
    ["active", state.active],
    ["flip", state.flip],
  ];
  for (const [name, image] of rows) {
    const values = samplePixel(image, x, y);
    const linear = viewer.querySelector(`[data-pixel-linear="${name}"]`);
    const srgb = viewer.querySelector(`[data-pixel-srgb="${name}"]`);
    if (linear) {
      const prefix = image?.transfer === "display" && values ? "display RGB  " : "";
      linear.textContent = `${prefix}${formatFloatTriplet(values)}`;
    }
    if (srgb) srgb.textContent = formatByteTriplet(values ? srgbBytesFor(image, ...values) : null);
  }
}

function pointerPixel(canvas, image, event) {
  const rect = canvas.getBoundingClientRect();
  const x = Math.max(0, Math.min(image.width - 1, Math.floor((event.clientX - rect.left) * image.width / rect.width)));
  const y = Math.max(0, Math.min(image.height - 1, Math.floor((event.clientY - rect.top) * image.height / rect.height)));
  return [x, y];
}

async function initializeViewer(viewer) {
  if (viewer.dataset.exrInitialized === "true") return;
  viewer.dataset.exrInitialized = "true";

  const status = viewer.querySelector("[data-exr-status]");
  const mode = viewer.querySelector("[data-comparison-mode]");
  const mainCanvas = viewer.querySelector("[data-main-canvas]");
  const zoomCanvas = viewer.querySelector("[data-zoom-canvas]");
  const flipCanvas = viewer.querySelector("[data-flip-canvas]");

  try {
    if (status) status.textContent = "Loading EXRs...";
    const [reference, render, flip] = await Promise.all([
      viewer.dataset.referenceSrc ? loadImageSource(viewer.dataset.referenceSrc, "linear") : Promise.resolve(null),
      viewer.dataset.renderSrc ? loadImageSource(viewer.dataset.renderSrc, "linear") : Promise.resolve(null),
      viewer.dataset.flipSrc ? loadImageSource(viewer.dataset.flipSrc, "display") : Promise.resolve(null),
    ]);
    const state = { reference, render, flip, active: reference || render, pointer: [0, 0] };
    viewer._typhoonExrState = state;

    if (!state.active) {
      if (status) status.textContent = "No EXR image available.";
      return;
    }

    drawDecoded(mainCanvas, state.active);
    if (flip) drawDecoded(flipCanvas, flip);
    drawZoom(zoomCanvas, state.active, 0, 0);
    updatePixelReadout(viewer, state, 0, 0);
    if (mode) mode.textContent = state.active === render ? "Render" : "Reference";
    if (status) status.textContent = "";

    mainCanvas.addEventListener("mousemove", (event) => {
      state.pointer = pointerPixel(mainCanvas, state.active, event);
      drawZoom(zoomCanvas, state.active, state.pointer[0], state.pointer[1]);
      updatePixelReadout(viewer, state, state.pointer[0], state.pointer[1]);
    });
  } catch (error) {
    viewer.dataset.exrInitialized = "false";
    if (status) status.textContent = String(error.message || error);
  }
}

function setActiveImage(viewer, imageName) {
  const state = viewer._typhoonExrState;
  if (!state) return;
  const next = imageName === "render" ? state.render : state.reference;
  if (!next) return;
  state.active = next;
  const mode = viewer.querySelector("[data-comparison-mode]");
  const mainCanvas = viewer.querySelector("[data-main-canvas]");
  const zoomCanvas = viewer.querySelector("[data-zoom-canvas]");
  drawDecoded(mainCanvas, state.active);
  drawZoom(zoomCanvas, state.active, state.pointer[0], state.pointer[1]);
  updatePixelReadout(viewer, state, state.pointer[0], state.pointer[1]);
  if (mode) mode.textContent = imageName === "render" ? "Render" : "Reference";
}

let hoveredViewer = null;
for (const viewer of document.querySelectorAll("[data-exr-viewer]")) {
  viewer.addEventListener("mouseenter", () => {
    hoveredViewer = viewer;
    initializeViewer(viewer);
  });
  viewer.addEventListener("mouseleave", () => {
    if (hoveredViewer === viewer) hoveredViewer = null;
  });
}

for (const row of document.querySelectorAll("tr.result-row[data-detail-row]")) {
  row.addEventListener("click", () => {
    window.setTimeout(() => {
      const detail = document.getElementById(row.dataset.detailRow);
      if (!detail || detail.hidden) return;
      for (const viewer of detail.querySelectorAll("[data-exr-viewer]")) {
        initializeViewer(viewer);
      }
    }, 0);
  });
}

document.addEventListener("keydown", (event) => {
  if (!hoveredViewer) return;
  if (event.key === "1") {
    event.preventDefault();
    setActiveImage(hoveredViewer, "reference");
  } else if (event.key === "2") {
    event.preventDefault();
    setActiveImage(hoveredViewer, "render");
  }
});

const thumbnailStrips = Array.from(document.querySelectorAll("[data-thumbnail-viewer]"));
if ("IntersectionObserver" in window) {
  const thumbnailObserver = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      thumbnailObserver.unobserve(entry.target);
      initializeThumbnailStrip(entry.target);
    }
  }, { rootMargin: "300px" });
  for (const strip of thumbnailStrips) thumbnailObserver.observe(strip);
} else {
  for (const strip of thumbnailStrips) initializeThumbnailStrip(strip);
}
