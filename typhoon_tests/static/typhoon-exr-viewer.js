const wasmUrl = new URL("typhoon_exr_wasm.wasm", import.meta.url);
let wasmPromise = null;
const imageCache = new Map();
const MAGMA_STOPS = [
  [0.0, 0.001462, 0.000466, 0.013866],
  [0.1, 0.078815, 0.054184, 0.211667],
  [0.2, 0.232077, 0.059889, 0.437695],
  [0.3, 0.390384, 0.100379, 0.501864],
  [0.4, 0.550287, 0.161158, 0.505719],
  [0.5, 0.716387, 0.214982, 0.475290],
  [0.6, 0.868793, 0.287728, 0.409303],
  [0.7, 0.967671, 0.439703, 0.359810],
  [0.8, 0.994738, 0.624350, 0.427397],
  [0.9, 0.995680, 0.812706, 0.572645],
  [1.0, 0.987053, 0.991438, 0.749504],
];

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

function clamp01(value) {
  if (!Number.isFinite(value)) return value > 0 ? 1 : 0;
  return Math.max(0, Math.min(1, value));
}

function toByte(value) {
  return Math.max(0, Math.min(255, Math.round(clamp01(value) * 255)));
}

function magmaColor(value) {
  const t = clamp01(value);
  for (let index = 1; index < MAGMA_STOPS.length; index += 1) {
    const previous = MAGMA_STOPS[index - 1];
    const next = MAGMA_STOPS[index];
    if (t <= next[0]) {
      const local = (t - previous[0]) / (next[0] - previous[0]);
      return [
        previous[1] + (next[1] - previous[1]) * local,
        previous[2] + (next[2] - previous[2]) * local,
        previous[3] + (next[3] - previous[3]) * local,
      ];
    }
  }
  return MAGMA_STOPS[MAGMA_STOPS.length - 1].slice(1);
}

function srgbBytesFor(image, r, g, b) {
  if (!image) return ["", "", ""];
  if (image.transfer === "magma") {
    const [magmaR, magmaG, magmaB] = magmaColor(r);
    return [toByte(magmaR), toByte(magmaG), toByte(magmaB)];
  }
  if (image.transfer === "display") return [toByte(r), toByte(g), toByte(b)];
  return [toByte(linearToSrgb(r)), toByte(linearToSrgb(g)), toByte(linearToSrgb(b))];
}

function axisContributions(sourceSize, targetSize, targetIndex) {
  if (targetSize < sourceSize) {
    const start = targetIndex * sourceSize / targetSize;
    const end = (targetIndex + 1) * sourceSize / targetSize;
    const span = end - start;
    const contributions = [];
    for (let sourceIndex = Math.floor(start); sourceIndex < Math.ceil(end); sourceIndex += 1) {
      const overlap = Math.min(end, sourceIndex + 1) - Math.max(start, sourceIndex);
      if (overlap > 0) contributions.push([sourceIndex, overlap / span]);
    }
    return contributions;
  }
  if (targetSize === sourceSize) return [[targetIndex, 1]];

  const source = (targetIndex + 0.5) * sourceSize / targetSize - 0.5;
  const source0 = Math.floor(source);
  const index0 = Math.max(0, Math.min(sourceSize - 1, source0));
  const index1 = Math.max(0, Math.min(sourceSize - 1, source0 + 1));
  if (index0 === index1) return [[index0, 1]];
  const mix = Math.max(0, Math.min(1, source - source0));
  return [[index0, 1 - mix], [index1, mix]];
}

function resizeDecodedImage(image, width, height) {
  if (!image || image.width === width && image.height === height) return image;
  if (width < 1 || height < 1 || image.width < 1 || image.height < 1) return image;

  const pixels = new Float32Array(width * height * 3);
  const xContributions = Array.from(
    { length: width }, (_, x) => axisContributions(image.width, width, x)
  );
  const yContributions = Array.from(
    { length: height }, (_, y) => axisContributions(image.height, height, y)
  );
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const destination = (y * width + x) * 3;
      for (let channel = 0; channel < 3; channel += 1) {
        let value = 0;
        for (const [sourceY, yWeight] of yContributions[y]) {
          for (const [sourceX, xWeight] of xContributions[x]) {
            value += image.pixels[
              (sourceY * image.width + sourceX) * 3 + channel
            ] * xWeight * yWeight;
          }
        }
        pixels[destination + channel] = value;
      }
    }
  }
  return { ...image, width, height, pixels };
}

function renderAtReferenceSize(render, reference) {
  if (!render || !reference) return render;
  return resizeDecodedImage(render, reference.width, reference.height);
}

async function loadReferenceSource(src) {
  if (!src) return { image: null, error: null };
  try {
    return { image: await loadImageSource(src, "linear"), error: null };
  } catch (error) {
    return { image: null, error };
  }
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

async function renderThumbnailCanvas(canvas) {
  const src = canvas.dataset.thumbnailSrc;
  if (!src) return;
  const transfer = canvas.dataset.thumbnailTransfer || "linear";
  const image = await loadImageSource(src, transfer);
  drawThumbnail(canvas, image);
}

async function initializeThumbnailStrip(strip) {
  if (strip.dataset.thumbnailsInitialized === "true") return;
  strip.dataset.thumbnailsInitialized = "true";
  const status = strip.querySelector("[data-thumbnail-status]");
  try {
    const canvases = Array.from(strip.querySelectorAll("[data-thumbnail-canvas]"));
    await Promise.all(canvases.map(renderThumbnailCanvas));
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
  return abs !== 0 && (abs < 0.001 || abs >= 10000) ? value.toExponential(3) : value.toFixed(3);
}

function formatFloatTriplet(values) {
  return values ? values.map(formatFloat).join("  ") : "";
}

function formatPixelValues(image, values) {
  if (!values) return "";
  return image?.transfer === "magma" ? formatFloat(values[0]) : formatFloatTriplet(values);
}

function formatByteTriplet(values) {
  return values ? values.join("  ") : "";
}

function referenceLabel(viewer) {
  return viewer.dataset.referenceLabel || "Reference";
}

function updateViewerLabels(viewer, state) {
  const mode = viewer.querySelector("[data-comparison-mode]");
  if (mode && state) {
    if (state.activeName === "flip") {
      mode.textContent = "FLIP";
    } else if (state.activeName === "render") {
      mode.textContent = "Render";
    } else {
      mode.textContent = referenceLabel(viewer);
    }
  }
  const referenceReadout = viewer.querySelector("[data-reference-readout-label]");
  if (referenceReadout) referenceReadout.textContent = referenceLabel(viewer);
  const target = viewer.querySelector("[data-comparison-target]");
  if (target) {
    const targetText = viewer.dataset.comparisonTarget || "Compare: Reference";
    target.textContent = targetText;
    target.title = targetText;
  }
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
    if (linear) linear.textContent = formatPixelValues(image, values);
    if (srgb) srgb.textContent = formatByteTriplet(values ? srgbBytesFor(image, ...values) : null);
  }
}

function pointerPixel(canvas, image, event) {
  const rect = canvas.getBoundingClientRect();
  const x = Math.max(0, Math.min(image.width - 1, Math.floor((event.clientX - rect.left) * image.width / rect.width)));
  const y = Math.max(0, Math.min(image.height - 1, Math.floor((event.clientY - rect.top) * image.height / rect.height)));
  return [x, y];
}

function reconcileViewerImages(state) {
  state.renderSource ||= state.render;
  state.render = renderAtReferenceSize(state.renderSource, state.reference);
  let active = state[state.activeName];
  if (!active) {
    if (state.reference) state.activeName = "reference";
    else if (state.render) state.activeName = "render";
    else state.activeName = "flip";
    active = state[state.activeName];
  }
  state.active = active || null;
}

function redrawViewer(viewer, state) {
  if (!state.active) return;
  state.pointer = [
    Math.max(0, Math.min(state.active.width - 1, state.pointer[0])),
    Math.max(0, Math.min(state.active.height - 1, state.pointer[1])),
  ];
  const mainCanvas = viewer.querySelector("[data-main-canvas]");
  const zoomCanvas = viewer.querySelector("[data-zoom-canvas]");
  drawDecoded(mainCanvas, state.active);
  drawZoom(zoomCanvas, state.active, state.pointer[0], state.pointer[1]);
  updatePixelReadout(viewer, state, state.pointer[0], state.pointer[1]);
  updateViewerLabels(viewer, state);
}

async function initializeViewer(viewer) {
  if (viewer.dataset.exrInitialized === "true") return;
  viewer.dataset.exrInitialized = "true";

  const status = viewer.querySelector("[data-exr-status]");
  const mode = viewer.querySelector("[data-comparison-mode]");
  const mainCanvas = viewer.querySelector("[data-main-canvas]");
  const zoomCanvas = viewer.querySelector("[data-zoom-canvas]");

  try {
    if (status) status.textContent = "Loading EXRs...";
    let referenceSrc = viewer.dataset.referenceSrc || "";
    const [initialReferenceResult, renderSource, flip] = await Promise.all([
      loadReferenceSource(referenceSrc),
      viewer.dataset.renderSrc ? loadImageSource(viewer.dataset.renderSrc, "linear") : Promise.resolve(null),
      viewer.dataset.flipSrc ? loadImageSource(viewer.dataset.flipSrc, "magma") : Promise.resolve(null),
    ]);
    let referenceResult = initialReferenceResult;
    while ((viewer.dataset.referenceSrc || "") !== referenceSrc) {
      referenceSrc = viewer.dataset.referenceSrc || "";
      referenceResult = await loadReferenceSource(referenceSrc);
    }
    const reference = referenceResult.image;
    const render = renderAtReferenceSize(renderSource, reference);
    const state = {
      reference,
      renderSource,
      render,
      flip,
      active: reference || render || flip,
      activeName: reference ? "reference" : (render ? "render" : "flip"),
      pointer: [0, 0],
    };
    viewer._typhoonExrState = state;

    if (!state.active) {
      if (status) status.textContent = "No EXR image available.";
      return;
    }

    redrawViewer(viewer, state);
    if (status) {
      status.textContent = referenceResult.error
        ? String(referenceResult.error.message || referenceResult.error)
        : "";
    }

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
  const nextName = imageName === "flip" ? "flip" : (imageName === "render" ? "render" : "reference");
  const next = nextName === "flip" ? state.flip : (nextName === "render" ? state.render : state.reference);
  if (!next) return;
  state.active = next;
  state.activeName = nextName;
  redrawViewer(viewer, state);
}

function readRunComparisonManifest() {
  const element = document.getElementById("typhoon-run-comparisons");
  if (!element) return { runs: [] };
  try {
    const parsed = JSON.parse(element.textContent || "{}");
    return { runs: Array.isArray(parsed.runs) ? parsed.runs : [] };
  } catch (_error) {
    return { runs: [] };
  }
}

const runComparisonManifest = readRunComparisonManifest();

function findComparisonRun(name) {
  return runComparisonManifest.runs.find((run) => run.name === name) || null;
}

function comparisonReferenceForCase(selectedRun, caseId, defaultSrc) {
  if (selectedRun) {
    const src = selectedRun.cases?.[caseId] || "";
    if (src) {
      return {
        src,
        label: selectedRun.name,
        target: `Compare: ${selectedRun.name}`,
        matched: true,
      };
    }
    return {
      src: defaultSrc || "",
      label: "Reference",
      target: `Compare: ${selectedRun.name} missing for this row; using Reference`,
      matched: false,
    };
  }
  return {
    src: defaultSrc || "",
    label: "Reference",
    target: "Compare: Reference",
    matched: false,
  };
}

async function reloadViewerReference(viewer) {
  const state = viewer._typhoonExrState;
  if (!state) return;
  const status = viewer.querySelector("[data-exr-status]");
  const src = viewer.dataset.referenceSrc || "";
  const token = `${src}|${viewer.dataset.referenceLabel || ""}`;
  viewer.dataset.referenceLoadToken = token;
  try {
    if (status) status.textContent = src ? "Loading comparison..." : "";
    const reference = src ? await loadImageSource(src, "linear") : null;
    if (viewer.dataset.referenceLoadToken !== token) return;
    state.reference = reference;
    reconcileViewerImages(state);
    redrawViewer(viewer, state);
    if (status) status.textContent = "";
  } catch (error) {
    if (viewer.dataset.referenceLoadToken !== token) return;
    state.reference = null;
    reconcileViewerImages(state);
    redrawViewer(viewer, state);
    if (status) status.textContent = String(error.message || error);
  }
}

function updateViewerComparison(viewer, selectedRun) {
  const caseId = viewer.dataset.caseId || "";
  const defaultSrc = viewer.dataset.defaultReferenceSrc || "";
  const comparison = comparisonReferenceForCase(selectedRun, caseId, defaultSrc);
  viewer.dataset.referenceSrc = comparison.src;
  viewer.dataset.referenceLabel = comparison.label;
  viewer.dataset.comparisonTarget = comparison.target;
  updateViewerLabels(viewer, viewer._typhoonExrState);
  reloadViewerReference(viewer);
  return comparison.matched;
}

async function refreshReferenceThumbnail(strip, selectedRun) {
  const canvas = strip.querySelector("[data-reference-thumbnail-canvas]");
  const link = strip.querySelector("[data-reference-thumbnail-link]");
  if (!canvas || !link) return false;
  const defaultSrc = canvas.dataset.defaultThumbnailSrc || "";
  const caseId = strip.dataset.caseId || "";
  const comparison = comparisonReferenceForCase(selectedRun, caseId, defaultSrc);
  const label = comparison.label;
  canvas.dataset.thumbnailSrc = comparison.src;
  canvas.dataset.thumbnailTransfer = "linear";
  canvas.setAttribute("aria-label", `${strip.dataset.caseLabel || "case"} ${label} thumbnail`);
  link.href = comparison.src || defaultSrc || "#";
  link.title = comparison.target;
  link.hidden = !comparison.src;
  if (strip.dataset.thumbnailsInitialized === "true" && comparison.src) {
    const status = strip.querySelector("[data-thumbnail-status]");
    try {
      await renderThumbnailCanvas(canvas);
      if (status) status.textContent = "";
    } catch (error) {
      if (status) status.textContent = String(error.message || error);
    }
  }
  return comparison.matched;
}

function initializeRunComparisonControls() {
  const select = document.querySelector("[data-run-comparison-select]");
  if (!select) return;
  const status = document.querySelector("[data-run-comparison-status]");
  const applySelection = () => {
    const selectedRun = findComparisonRun(select.value);
    let viewerCount = 0;
    let matched = 0;
    for (const viewer of document.querySelectorAll("[data-exr-viewer]")) {
      viewerCount += 1;
      if (updateViewerComparison(viewer, selectedRun)) matched += 1;
    }
    for (const strip of document.querySelectorAll("[data-thumbnail-viewer]")) {
      refreshReferenceThumbnail(strip, selectedRun);
    }
    if (!status) return;
    if (!selectedRun) {
      status.textContent = "";
    } else {
      status.textContent = `${matched}/${viewerCount} rows matched ${selectedRun.name}`;
    }
  };
  select.addEventListener("change", applySelection);
  applySelection();
}


export function reportRowFromControl(control) {
  return {
    suite: control.dataset.suite || "",
    key: control.dataset.key || "",
    usd: control.dataset.usdPath || "",
    reference: control.dataset.referencePath || "",
    render: control.dataset.renderPath || "",
    flipMean: control.dataset.flipMean || "",
  };
}

function selectedReportRows() {
  return Array.from(document.querySelectorAll("[data-result-select]:checked"))
    .map(reportRowFromControl);
}

function currentRunPath() {
  return new URL(".", window.location.href).pathname;
}

function setReportActionStatus(message) {
  const status = document.querySelector("[data-report-action-status]");
  if (status) status.textContent = message || "";
}

function setActionStatus(status, message) {
  if (status) {
    status.textContent = message || "";
  } else {
    setReportActionStatus(message);
  }
}

function reportUiStateKey() {
  return `typhoon-report-ui:${window.location.pathname}`;
}

export function captureReportUiState() {
  const sorts = [];
  for (const table of document.querySelectorAll("table[data-sortable-table]")) {
    const button = table.querySelector("th button[data-sort-direction]");
    if (!button) continue;
    sorts.push({
      key: table.dataset.sortTableKey || "",
      column: Number(button.dataset.sortColumn),
      direction: button.dataset.sortDirection === "desc" ? "desc" : "asc",
    });
  }
  const expanded = Array.from(
    document.querySelectorAll('tr.result-row[aria-expanded="true"]')
  ).map((row) => row.dataset.caseId || "").filter(Boolean);
  const selected = Array.from(
    document.querySelectorAll("[data-result-select]:checked")
  ).map((checkbox) => checkbox.dataset.caseId || "").filter(Boolean);
  const sections = {};
  for (const section of document.querySelectorAll("details[data-section-id]")) {
    sections[section.dataset.sectionId || ""] = section.open === true;
  }
  try {
    const storage = window.sessionStorage;
    storage.setItem(reportUiStateKey(), JSON.stringify({
      sorts,
      expanded,
      selected,
      sections,
      scrollX: window.scrollX || 0,
      scrollY: window.scrollY || 0,
    }));
  } catch (_error) {
    // Reloading still works when browser storage is unavailable.
  }
}

function selectableRowsForSelectAll(selectAll) {
  const table = selectAll?.closest?.("table[data-sortable-table]");
  const root = table || document;
  return Array.from(root.querySelectorAll("[data-result-select]"));
}

function updateSelectAllControl(selectAll) {
  const checkboxes = selectableRowsForSelectAll(selectAll);
  const selected = checkboxes.filter((checkbox) => checkbox.checked);
  selectAll.checked = checkboxes.length > 0 && selected.length === checkboxes.length;
  selectAll.indeterminate = selected.length > 0 && selected.length < checkboxes.length;
}

function updateSelectionControls() {
  const checkboxes = Array.from(document.querySelectorAll("[data-result-select]"));
  const selected = checkboxes.filter((checkbox) => checkbox.checked);
  const actions = document.querySelector("[data-selection-actions]");
  if (actions) actions.hidden = selected.length === 0;
  const thresholdButton = document.querySelector("[data-update-threshold]");
  if (thresholdButton) thresholdButton.textContent = `Update threshold (${selected.length})`;
  const referenceButton = document.querySelector("[data-update-reference]");
  if (referenceButton) referenceButton.textContent = `Update reference (${selected.length})`;
  for (const selectAll of document.querySelectorAll("[data-select-all]")) {
    updateSelectAllControl(selectAll);
  }
  if (selected.length === 0) setReportActionStatus("");
}

async function runReportAction(
  endpoint,
  button,
  actionLabel,
  rows = selectedReportRows(),
  status = null,
) {
  if (!rows.length) return;
  button.disabled = true;
  setActionStatus(status, `${actionLabel}...`);
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run: currentRunPath(), rows }),
    });
    let body = {};
    try {
      body = await response.json();
    } catch (_error) {
      body = {};
    }
    if (!response.ok || body.ok !== true) {
      throw new Error(body.error || `HTTP ${response.status}`);
    }
    const count = body.updated ?? rows.length;
    captureReportUiState();
    setActionStatus(
      status,
      `Updated ${count} row${count === 1 ? "" : "s"}; reloading...`,
    );
    window.setTimeout(() => window.location.reload(), 250);
  } catch (error) {
    setActionStatus(status, `Failed: ${error.message || error}`);
    button.disabled = false;
  }
}

function initializeSelectionControls() {
  const selectAllControls = Array.from(document.querySelectorAll("[data-select-all]"));
  const checkboxes = Array.from(document.querySelectorAll("[data-result-select]"));
  for (const selectAll of selectAllControls) {
    selectAll.addEventListener("change", () => {
      for (const checkbox of selectableRowsForSelectAll(selectAll)) {
        checkbox.checked = selectAll.checked;
      }
      updateSelectionControls();
    });
  }
  for (const checkbox of checkboxes) {
    checkbox.addEventListener("change", updateSelectionControls);
    checkbox.addEventListener("click", (event) => event.stopPropagation());
  }
  const thresholdButton = document.querySelector("[data-update-threshold]");
  if (thresholdButton) {
    thresholdButton.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      runReportAction("/__typhoon__/thresholds", thresholdButton, "Updating thresholds");
    });
  }
  const referenceButton = document.querySelector("[data-update-reference]");
  if (referenceButton) {
    referenceButton.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      runReportAction("/__typhoon__/references", referenceButton, "Updating references");
    });
  }
  const initializeRowAction = (selector, endpoint, actionLabel, prepareRow = (row) => row) => {
    for (const button of document.querySelectorAll(selector)) {
      button.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        const detail = button.closest("tr.result-detail-row");
        const resultRow = detail?.previousElementSibling;
        const control = resultRow?.querySelector("[data-result-select]");
        if (!control) return;
        const status = button.closest(".detail-actions")
          ?.querySelector("[data-detail-action-status]");
        const row = prepareRow(reportRowFromControl(control), button);
        const label = typeof actionLabel === "function" ? actionLabel(button) : actionLabel;
        runReportAction(
          endpoint,
          button,
          label,
          [row],
          status,
        );
      });
    }
  };
  initializeRowAction(
    "[data-row-update-threshold]",
    "/__typhoon__/thresholds",
    "Updating threshold",
  );
  initializeRowAction(
    "[data-row-update-reference]",
    "/__typhoon__/references",
    "Updating reference",
  );
  initializeRowAction(
    "[data-row-update-suspect]",
    "/__typhoon__/suspects",
    (button) => button.dataset.suspectTarget === "true" ? "Marking suspect" : "Clearing suspect",
    (row, button) => ({
      ...row,
      suspect: button.dataset.suspectTarget === "true",
    }),
  );
  updateSelectionControls();
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

async function openUsdview(button) {
  const actions = button.closest(".detail-actions");
  const status = actions ? actions.querySelector("[data-detail-action-status]") : null;
  const payload = {
    usd: button.dataset.usdPath || "",
    camera: button.dataset.cameraPath || "",
    frame: button.dataset.frame || "",
  };
  button.disabled = true;
  if (status) status.textContent = "Opening usdview...";
  try {
    const response = await fetch("/__typhoon__/usdview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    let body = {};
    try {
      body = await response.json();
    } catch (_error) {
      body = {};
    }
    if (!response.ok || body.ok !== true) {
      throw new Error(body.error || `HTTP ${response.status}`);
    }
    if (status) status.textContent = "Opened in usdview";
  } catch (error) {
    if (status) status.textContent = `Failed to open usdview: ${error.message || error}`;
  } finally {
    button.disabled = false;
  }
}

for (const button of document.querySelectorAll("[data-usdview-open]")) {
  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    openUsdview(button);
  });
}

async function restoreReportViewport() {
  const state = window.__typhoonRestoredReportState;
  if (!state) return;
  const viewers = Array.from(
    document.querySelectorAll(
      "tr.result-detail-row:not([hidden]) [data-exr-viewer]"
    )
  );
  await Promise.all(viewers.map((viewer) => initializeViewer(viewer)));
  const nextFrame = () => new Promise((resolve) => {
    if (typeof window.requestAnimationFrame === "function") {
      window.requestAnimationFrame(resolve);
    } else {
      window.setTimeout(resolve, 0);
    }
  });
  await nextFrame();
  await nextFrame();
  if (typeof window.scrollTo === "function") {
    window.scrollTo(Number(state.scrollX) || 0, Number(state.scrollY) || 0);
  }
  delete window.__typhoonRestoredReportState;
}

document.addEventListener("keydown", (event) => {
  if (!hoveredViewer) return;
  if (event.key === "1") {
    event.preventDefault();
    setActiveImage(hoveredViewer, "reference");
  } else if (event.key === "2") {
    event.preventDefault();
    setActiveImage(hoveredViewer, "render");
  } else if (event.key === "3") {
    event.preventDefault();
    setActiveImage(hoveredViewer, "flip");
  }
});

initializeRunComparisonControls();
initializeSelectionControls();
restoreReportViewport();

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

export {
  magmaColor,
  srgbBytesFor,
  formatPixelValues,
  resizeDecodedImage,
  renderAtReferenceSize,
  initializeViewer,
};
