// @ts-nocheck
// Custom Google Maps-compatible engine powered by Google Maps tiles & the CherryPro software bridge
import { normalizeLongitude } from './geographicCoordinates';

const TILE_SIZE = 256;

function clampLat(lat) {
  return Math.max(-85.05112878, Math.min(85.05112878, Number(lat) || 0));
}

function latLngToWorldPoint(lat, lng) {
  const siny = Math.min(Math.max(Math.sin((clampLat(lat) * Math.PI) / 180), -0.9999), 0.9999);
  return {
    x: TILE_SIZE * (0.5 + normalizeLongitude(lng) / 360),
    y: TILE_SIZE * (0.5 - Math.log((1 + siny) / (1 - siny)) / (4 * Math.PI)),
  };
}

function worldPointToLatLng(x, y) {
  const lng = normalizeLongitude(((x / TILE_SIZE - 0.5) * 360));
  const n = Math.PI - (2 * Math.PI * y) / TILE_SIZE;
  const lat = (180 / Math.PI) * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)));
  return { lat: clampLat(lat), lng };
}

class LatLng {
  constructor(lat, lng) {
    if (typeof lat === 'object' && lat !== null) {
      this._lat = Number(lat.lat);
      this._lng = Number(lat.lng);
    } else {
      this._lat = Number(lat);
      this._lng = Number(lng);
    }
  }
  lat() { return this._lat; }
  lng() { return this._lng; }
  toJSON() { return { lat: this._lat, lng: this._lng }; }
}

class LatLngBounds {
  constructor(sw, ne) {
    this.south = 90;
    this.north = -90;
    this.west = 180;
    this.east = -180;
    this._empty = true;
    if (sw && ne) {
      this.extend(sw);
      this.extend(ne);
    } else if (sw && typeof sw === 'object' && 'south' in sw) {
      this.south = Number(sw.south);
      this.north = Number(sw.north);
      this.west = Number(sw.west);
      this.east = Number(sw.east);
      this._empty = false;
    }
  }
  extend(point) {
    const lat = typeof point.lat === 'function' ? point.lat() : Number(point.lat);
    const lng = typeof point.lng === 'function' ? point.lng() : Number(point.lng);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return this;
    if (this._empty) {
      this.south = lat;
      this.north = lat;
      this.west = lng;
      this.east = lng;
      this._empty = false;
    } else {
      this.south = Math.min(this.south, lat);
      this.north = Math.max(this.north, lat);
      this.west = Math.min(this.west, lng);
      this.east = Math.max(this.east, lng);
    }
    return this;
  }
  getNorthEast() { return new LatLng(this.north, this.east); }
  getSouthWest() { return new LatLng(this.south, this.west); }
  contains(pt) {
    const lat = typeof pt.lat === 'function' ? pt.lat() : Number(pt.lat);
    const lng = typeof pt.lng === 'function' ? pt.lng() : Number(pt.lng);
    if (this._empty) return false;
    const inLat = lat >= this.south && lat <= this.north;
    const inLng = this.west <= this.east ? (lng >= this.west && lng <= this.east) : (lng >= this.west || lng <= this.east);
    return inLat && inLng;
  }
  toJSON() {
    if (this._empty) return { south: -80, north: 85, west: -180, east: 180 };
    return { south: this.south, north: this.north, west: this.west, east: this.east };
  }
}

class Size {
  constructor(width, height) {
    this.width = width;
    this.height = height;
  }
}

class Point {
  constructor(x, y) {
    this.x = x;
    this.y = y;
  }
}

class Emitter {
  constructor() {
    this._listeners = new Map();
  }
  addListener(eventName, handler) {
    if (!this._listeners.has(eventName)) this._listeners.set(eventName, new Set());
    const set = this._listeners.get(eventName);
    set.add(handler);
    return { remove: () => set.delete(handler) };
  }
  _emit(eventName, payload) {
    const set = this._listeners.get(eventName);
    if (!set) return;
    set.forEach(fn => {
      try { fn(payload); } catch { /* ignore listener errors */ }
    });
  }
  _clearListeners() {
    this._listeners.clear();
  }
}

function getTileUrl(mapTypeId, z, x, y, fallbackStep = 0) {
  if (fallbackStep === 0) {
    const subdomain = ['mt0', 'mt1', 'mt2', 'mt3'][Math.abs(x + y) % 4];
    const lyrs = mapTypeId === 'satellite' ? 's,h' : mapTypeId === 'terrain' ? 'p' : 'm';
    return `https://${subdomain}.google.com/vt/lyrs=${lyrs}&x=${x}&y=${y}&z=${z}&hl=en&scale=2`;
  }
  if (fallbackStep === 1) {
    const subdomain = ['mt2', 'mt3', 'mt0', 'mt1'][Math.abs(x + y) % 4];
    const lyrs = mapTypeId === 'satellite' ? 's' : mapTypeId === 'terrain' ? 'p' : 'r';
    return `https://${subdomain}.google.com/vt/lyrs=${lyrs}&x=${x}&y=${y}&z=${z}&hl=en`;
  }
  if (mapTypeId === 'satellite') {
    return `https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/${z}/${y}/${x}`;
  }
  const cartoSub = ['a', 'b', 'c', 'd'][Math.abs(x + y) % 4];
  return `https://${cartoSub}.basemaps.cartocdn.com/rastertiles/voyager/${z}/${x}/${y}@2x.png`;
}

class CustomGoogleMap extends Emitter {
  constructor(container, opts = {}) {
    super();
    this._container = container;
    this._center = new LatLng(opts.center?.lat ?? 20, opts.center?.lng ?? 0);
    this._zoom = Math.max(2, Math.min(20, Math.round(opts.zoom ?? 2)));
    this._mapTypeId = opts.mapTypeId || 'roadmap';
    this._tilt = 0;
    this._heading = 0;
    this._markers = new Set();
    this._polylines = new Set();
    this._infoWindows = new Set();
    this._tileCache = new Map();
    this._rafId = null;
    this._idleTimer = null;
    this._lastWheelAt = 0;

    this._projection = {
      fromLatLngToPoint: latLng => {
        const lat = typeof latLng.lat === 'function' ? latLng.lat() : latLng.lat;
        const lng = typeof latLng.lng === 'function' ? latLng.lng() : latLng.lng;
        const pt = latLngToWorldPoint(lat, lng);
        return new Point(pt.x, pt.y);
      },
    };

    this._setupDOM();
    this._bindInteractions();
    this._render();
    this._scheduleIdle(40);
  }

  _scheduleRender() {
    if (this._rafId !== null) return;
    this._rafId = requestAnimationFrame(() => {
      this._rafId = null;
      this._render();
    });
  }

  _scheduleIdle(delay = 90) {
    if (this._idleTimer) clearTimeout(this._idleTimer);
    this._idleTimer = setTimeout(() => {
      this._idleTimer = null;
      this._emit('idle');
    }, delay);
  }

  _setupDOM() {
    this._container.innerHTML = '';
    const root = document.createElement('div');
    root.style.cssText = 'position:relative;width:100%;height:100%;overflow:hidden;background:#cfe2e8;user-select:none;touch-action:none;cursor:grab;z-index:0;isolation:isolate;';
    this._root = root;

    const scene = document.createElement('div');
    scene.style.cssText = 'position:absolute;inset:0;transform-style:preserve-3d;z-index:1;';
    this._scene = scene;

    const baseLayer = document.createElement('div');
    baseLayer.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:0;';
    this._baseLayer = baseLayer;

    const backdropPane = document.createElement('div');
    backdropPane.style.cssText = 'position:absolute;inset:0;pointer-events:none;z-index:1;overflow:hidden;';
    this._backdropPane = backdropPane;

    const tilePane = document.createElement('div');
    tilePane.style.cssText = 'position:absolute;inset:0;pointer-events:none;z-index:2;overflow:hidden;';
    this._tilePane = tilePane;

    const svgOverlay = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svgOverlay.setAttribute('style', 'position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:3;');
    this._svgOverlay = svgOverlay;

    const markerPane = document.createElement('div');
    markerPane.style.cssText = 'position:absolute;inset:0;pointer-events:none;z-index:4;';
    this._markerPane = markerPane;

    const infoPane = document.createElement('div');
    infoPane.style.cssText = 'position:absolute;inset:0;pointer-events:none;z-index:5;';
    this._infoPane = infoPane;

    const badge = document.createElement('div');
    badge.style.cssText = 'position:absolute;bottom:6px;left:10px;z-index:6;font-size:9px;color:#42594e;background:rgba(255,254,250,0.88);padding:2.5px 7px;border-radius:4px;border:1px solid rgba(215,228,220,0.9);pointer-events:none;font-family:Manrope,sans-serif;font-weight:600;';
    badge.textContent = 'Google Maps · CherryPro Software Bridge Basemap';
    this._badge = badge;

    root.append(scene, infoPane, badge);
    scene.append(baseLayer, backdropPane, tilePane, svgOverlay, markerPane);
    this._container.appendChild(root);

    if (typeof ResizeObserver !== 'undefined') {
      this._ro = new ResizeObserver(() => this._render());
      this._ro.observe(this._container);
    }
  }

  _bindInteractions() {
    let dragging = false;
    let startX = 0;
    let startY = 0;
    let startWorld = null;
    let moved = false;
    let downTarget = null;
    let activePointerId = null;

    const startDrag = (clientX, clientY, target) => {
      if (target?.closest?.('.marker-popup')) return false;
      dragging = true;
      moved = false;
      downTarget = target;
      startX = clientX;
      startY = clientY;
      startWorld = latLngToWorldPoint(this._center.lat(), this._center.lng());
      this._root.style.cursor = 'grabbing';
      return true;
    };

    const moveDrag = (clientX, clientY) => {
      if (!dragging || !startWorld) return;
      const dx = clientX - startX;
      const dy = clientY - startY;
      if (Math.abs(dx) > 4 || Math.abs(dy) > 4) moved = true;
      if (!moved) return;
      const scale = 2 ** this._zoom;
      const worldWidth = TILE_SIZE * scale;
      const tilt = (this._tilt || 0) * Math.PI / 180;
      const heading = (this._heading || 0) * Math.PI / 180;
      const cos = Math.cos(heading);
      const sin = Math.sin(heading);
      const sy = dy / Math.max(0.45, Math.cos(tilt));
      const rdx = dx * cos + sy * sin;
      const rdy = -dx * sin + sy * cos;
      const wx = ((startWorld.x * scale - rdx) % worldWidth + worldWidth) % worldWidth / scale;
      const wy = Math.max(4, Math.min(TILE_SIZE - 4, (startWorld.y * scale - rdy) / scale));
      const next = worldPointToLatLng(wx, wy);
      this._center = new LatLng(next.lat, next.lng);
      this._scheduleRender();
    };

    const endDrag = target => {
      if (!dragging) return;
      const wasMoved = moved;
      const tapTarget = downTarget || target;
      dragging = false;
      moved = false;
      downTarget = null;
      activePointerId = null;
      this._root.style.cursor = 'grab';
      if (wasMoved) {
        this._render();
        this._scheduleIdle(60);
      } else {
        const markerEl = tapTarget?.closest?.('[data-cherry-marker="true"]');
        if (markerEl && markerEl._cherryMarker) {
          markerEl._cherryMarker._emit('click', {});
        } else if (!tapTarget?.closest?.('[data-cherry-interactive="true"]')) {
          this._emit('click', {});
        }
      }
    };

    const onPointerDown = e => {
      if (e.pointerType === 'touch') return;
      if (startDrag(e.clientX, e.clientY, e.target)) {
        activePointerId = e.pointerId;
      }
    };

    const onPointerMove = e => {
      if (e.pointerType === 'touch') return;
      if (activePointerId !== null && e.pointerId !== activePointerId) return;
      const prevMoved = moved;
      moveDrag(e.clientX, e.clientY);
      if (!prevMoved && moved && activePointerId !== null) {
        try { this._root.setPointerCapture(activePointerId); } catch { /* ignore */ }
      }
    };

    const onPointerUp = e => {
      if (e.pointerType === 'touch') return;
      if (activePointerId !== null && e.pointerId !== activePointerId) return;
      if (moved) {
        try { this._root.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
      }
      endDrag(e.target);
    };

    this._root.addEventListener('pointerdown', onPointerDown);
    this._root.addEventListener('pointermove', onPointerMove);
    this._root.addEventListener('pointerup', onPointerUp);
    this._root.addEventListener('pointercancel', onPointerUp);
    window.addEventListener('pointerup', onPointerUp);

    let touchStartDist = 0;
    let touchStartZoom = 2;
    let lastTapTime = 0;
    this._root.addEventListener('touchstart', e => {
      if (e.target?.closest?.('.marker-popup')) return;
      if (e.touches.length === 1) {
        startDrag(e.touches[0].clientX, e.touches[0].clientY, e.target);
      } else if (e.touches.length === 2) {
        dragging = false;
        const dx = e.touches[0].clientX - e.touches[1].clientX;
        const dy = e.touches[0].clientY - e.touches[1].clientY;
        touchStartDist = Math.hypot(dx, dy);
        touchStartZoom = this._zoom;
      }
    }, { passive: false });

    this._root.addEventListener('touchmove', e => {
      if (e.target?.closest?.('.marker-popup')) return;
      if (e.cancelable) e.preventDefault();
      if (e.touches.length === 1 && dragging) {
        moveDrag(e.touches[0].clientX, e.touches[0].clientY);
      } else if (e.touches.length === 2 && touchStartDist > 0) {
        const dx = e.touches[0].clientX - e.touches[1].clientX;
        const dy = e.touches[0].clientY - e.touches[1].clientY;
        const dist = Math.hypot(dx, dy);
        const ratio = dist / touchStartDist;
        const zoomDelta = Math.round(Math.log2(ratio) * 1.4);
        const nextZoom = Math.max(2, Math.min(20, touchStartZoom + zoomDelta));
        if (nextZoom !== this._zoom) {
          this.setZoom(nextZoom);
        }
      }
    }, { passive: false });

    this._root.addEventListener('touchend', e => {
      if (e.touches.length === 0) {
        const now = Date.now();
        const wasMoved = moved;
        const tapTarget = downTarget || e.target;
        touchStartDist = 0;
        endDrag(e.target);
        if (!wasMoved && !tapTarget?.closest?.('[data-cherry-marker="true"]') && now - lastTapTime < 300) {
          this.setZoom(Math.min(20, this._zoom + 1));
          lastTapTime = 0;
        } else if (!wasMoved) {
          lastTapTime = now;
        }
      } else if (e.touches.length === 1) {
        touchStartDist = 0;
        startDrag(e.touches[0].clientX, e.touches[0].clientY, e.target);
      }
    }, { passive: true });

    this._root.addEventListener('wheel', e => {
      e.preventDefault();
      const now = performance.now();
      if (now - this._lastWheelAt < 55) return;
      this._lastWheelAt = now;
      const delta = e.deltaY < 0 ? 1 : -1;
      const nextZoom = Math.max(2, Math.min(20, this._zoom + delta));
      if (nextZoom !== this._zoom) {
        this.setZoom(nextZoom);
      }
    }, { passive: false });

    this._root.addEventListener('dblclick', e => {
      if (e.target.closest('[data-cherry-interactive="true"]')) return;
      this.setZoom(Math.min(20, this._zoom + 1));
    });
  }

  getCenter() { return this._center; }
  getZoom() { return this._zoom; }
  getProjection() { return this._projection; }

  getBounds() {
    const w = this._container.clientWidth || 960;
    const h = this._container.clientHeight || 640;
    const scale = 2 ** this._zoom;
    const centerWorld = latLngToWorldPoint(this._center.lat(), this._center.lng());
    const cx = centerWorld.x * scale;
    const cy = centerWorld.y * scale;
    const top = worldPointToLatLng(centerWorld.x, Math.max(0, (cy - h / 2) / scale));
    const bottom = worldPointToLatLng(centerWorld.x, Math.min(TILE_SIZE, (cy + h / 2) / scale));
    const halfLngSpan = Math.min(179.9, (w / (TILE_SIZE * scale)) * 180);
    if (halfLngSpan >= 175) {
      return new LatLngBounds({ south: bottom.lat, north: top.lat, west: -180, east: 180 });
    }
    const west = normalizeLongitude(this._center.lng() - halfLngSpan);
    const east = normalizeLongitude(this._center.lng() + halfLngSpan);
    return new LatLngBounds({
      south: Math.min(bottom.lat, top.lat),
      north: Math.max(bottom.lat, top.lat),
      west,
      east,
    });
  }

  getTilt() { return this._tilt || 0; }
  getHeading() { return this._heading || 0; }

  setTilt(tilt) {
    const next = Math.max(0, Math.min(65, Math.round(Number(tilt) || 0)));
    if (next === this._tilt) return;
    this._tilt = next;
    this._applyCamera();
  }

  setHeading(heading) {
    const next = ((Math.round(Number(heading) || 0) % 360) + 360) % 360;
    if (next === this._heading) return;
    this._heading = next;
    this._applyCamera();
  }

  _applyCamera() {
    if (!this._scene) return;
    const tilt = this._tilt || 0;
    const heading = this._heading || 0;
    this._scene.style.transformOrigin = '50% 62%';
    this._scene.style.transition = 'transform 160ms ease-out';
    this._scene.style.transform = tilt || heading
      ? `perspective(980px) rotateX(${tilt}deg) rotateZ(${-heading}deg)`
      : 'none';
  }

  setZoom(zoom) {
    const next = Math.max(2, Math.min(20, Math.round(Number(zoom) || 2)));
    if (next === this._zoom) return;
    this._zoom = next;
    this._scheduleRender();
    this._emit('zoom_changed');
    this._scheduleIdle(70);
  }

  panTo(pos) {
    const lat = typeof pos.lat === 'function' ? pos.lat() : Number(pos.lat);
    const lng = typeof pos.lng === 'function' ? pos.lng() : Number(pos.lng);
    this._center = new LatLng(clampLat(lat), normalizeLongitude(lng));
    this._scheduleRender();
    this._scheduleIdle(70);
  }

  setMapTypeId(typeId) {
    const nextType = typeId || 'roadmap';
    if (this._mapTypeId === nextType) return;
    this._mapTypeId = nextType;
    // Keep existing loaded tiles in cache as backdrop until new style tiles finish loading (prevents gray flash)
    this._scheduleRender();
  }

  fitBounds(boundsInput, padding = {}) {
    const b = boundsInput instanceof LatLngBounds ? boundsInput.toJSON() : boundsInput;
    if (!b) return;
    const south = Number(b.south ?? -60);
    const north = Number(b.north ?? 75);
    const west = Number(b.west ?? -180);
    const east = Number(b.east ?? 180);
    const latCenter = clampLat((south + north) / 2);
    const lngCenter = west <= east ? (west + east) / 2 : normalizeLongitude((west + (east + 360)) / 2);

    const w = Math.max(240, (this._container.clientWidth || 960) - (padding.left || 0) - (padding.right || 0));
    const h = Math.max(200, (this._container.clientHeight || 640) - (padding.top || 0) - (padding.bottom || 0));
    const latSpan = Math.max(0.001, Math.abs(north - south));
    const lngSpan = Math.max(0.001, west <= east ? east - west : (180 - west) + (east + 180));

    let bestZoom = 2;
    for (let z = 18; z >= 2; z--) {
      const scale = 2 ** z;
      const neededW = (lngSpan / 360) * TILE_SIZE * scale;
      const neededH = (latSpan / 170) * TILE_SIZE * scale;
      if (neededW <= w && neededH <= h) {
        bestZoom = z;
        break;
      }
    }
    this._center = new LatLng(latCenter, lngCenter);
    this._zoom = bestZoom;
    this._tilt = 0;
    this._heading = 0;
    this._applyCamera();
    this._scheduleRender();
    this._emit('zoom_changed');
    this._scheduleIdle(70);
  }

  _latLngToContainerPixel(lat, lng) {
    const w = this._container.clientWidth || 960;
    const h = this._container.clientHeight || 640;
    const scale = 2 ** this._zoom;
    const worldWidth = TILE_SIZE * scale;
    const cWorld = latLngToWorldPoint(this._center.lat(), this._center.lng());
    const pWorld = latLngToWorldPoint(lat, lng);
    let dx = (pWorld.x - cWorld.x) * scale;
    dx = ((dx + worldWidth / 2) % worldWidth + worldWidth) % worldWidth - worldWidth / 2;
    const dy = (pWorld.y - cWorld.y) * scale;
    return { x: w / 2 + dx, y: h / 2 + dy };
  }

  _render() {
    this._renderBaseCanvas();
    this._renderTiles();
    this._renderPolylines();
    this._renderMarkers();
    this._renderInfoWindows();
  }

  _renderBaseCanvas() {
    const layer = this._baseLayer;
    if (!layer) return;
    const isSat = this._mapTypeId === 'satellite';
    const isTerrain = this._mapTypeId === 'terrain';
    // Solid cartographic wash so pan/zoom never flashes gray before tiles decode.
    layer.style.background = isSat
      ? 'linear-gradient(#132a38, #193545 50%, #122734)'
      : isTerrain
        ? 'linear-gradient(#cce0e6, #eae8dc 50%, #cde0e6)'
        : 'linear-gradient(#cbe0e7, #f2efe6 50%, #cbe0e7)';
  }

  _renderTiles() {
    const w = this._container.clientWidth || 960;
    const h = this._container.clientHeight || 640;
    const z = this._zoom;
    const scale = 2 ** z;
    const cWorld = latLngToWorldPoint(this._center.lat(), this._center.lng());
    const centerPxX = cWorld.x * scale;
    const centerPxY = cWorld.y * scale;

    // Visible viewport tile range
    const visMinX = Math.floor((centerPxX - w / 2) / TILE_SIZE);
    const visMaxX = Math.floor((centerPxX + w / 2) / TILE_SIZE);
    const visMinY = Math.max(0, Math.floor((centerPxY - h / 2) / TILE_SIZE));
    const visMaxY = Math.min(scale - 1, Math.floor((centerPxY + h / 2) / TILE_SIZE));

    // Prefetch 1-tile ring outside visible viewport for smooth panning
    const minTileX = visMinX - 1;
    const maxTileX = visMaxX + 1;
    const minTileY = Math.max(0, visMinY - 1);
    const maxTileY = Math.min(scale - 1, visMaxY + 1);

    const activeKeys = new Set();
    const now = performance.now();
    let allVisibleLoaded = true;

    for (let tx = minTileX; tx <= maxTileX; tx++) {
      const wrappedX = ((tx % scale) + scale) % scale;
      for (let ty = minTileY; ty <= maxTileY; ty++) {
        const key = `${this._mapTypeId}:${z}:${tx}:${ty}`;
        activeKeys.add(key);
        let entry = this._tileCache.get(key);
        if (!entry) {
          const img = document.createElement('img');
          img.alt = '';
          img.draggable = false;
          img.decoding = 'async';
          img.style.cssText = `position:absolute;left:0;top:0;width:${TILE_SIZE}px;height:${TILE_SIZE}px;max-width:none;border:0;opacity:0;transition:opacity 0.12s ease-out;will-change:transform,opacity;`;
          entry = {
            key,
            mapTypeId: this._mapTypeId,
            z,
            tx,
            ty,
            wrappedX,
            img,
            loaded: false,
            fallbackStep: 0,
            lastUsed: now,
          };
          img.onload = () => {
            entry.loaded = true;
            img.style.opacity = '1';
            this._scheduleRender();
          };
          img.onerror = () => {
            if (entry.fallbackStep < 2) {
              entry.fallbackStep += 1;
              img.src = getTileUrl(entry.mapTypeId, entry.z, entry.wrappedX, entry.ty, entry.fallbackStep);
            }
          };
          img.src = getTileUrl(this._mapTypeId, z, wrappedX, ty, 0);
          this._tilePane.appendChild(img);
          this._tileCache.set(key, entry);
        }

        entry.lastUsed = now;
        if (entry.img.parentNode !== this._tilePane) {
          this._tilePane.appendChild(entry.img);
        }
        entry.img.style.display = 'block';
        entry.img.style.width = `${TILE_SIZE}px`;
        entry.img.style.height = `${TILE_SIZE}px`;
        const left = Math.round(w / 2 + (tx * TILE_SIZE - centerPxX));
        const top = Math.round(h / 2 + (ty * TILE_SIZE - centerPxY));
        entry.img.style.transform = `translate3d(${left}px, ${top}px, 0)`;

        const isVisibleTile = tx >= visMinX && tx <= visMaxX && ty >= visMinY && ty <= visMaxY;
        if (isVisibleTile && !entry.loaded) {
          allVisibleLoaded = false;
        }
      }
    }

    // Retain already-loaded tiles from previous zooms/styles in backdropPane while current zoom tiles load
    for (const [key, entry] of this._tileCache.entries()) {
      if (activeKeys.has(key)) continue;
      if (!entry.loaded) {
        if (now - entry.lastUsed > 6000) {
          entry.img.remove();
          this._tileCache.delete(key);
        }
        continue;
      }

      if (!allVisibleLoaded && Math.abs(z - entry.z) <= 6) {
        const scaleRatio = 2 ** (z - entry.z);
        const scaledSize = TILE_SIZE * scaleRatio;
        const left = w / 2 + (entry.tx * scaledSize - centerPxX);
        const top = h / 2 + (entry.ty * scaledSize - centerPxY);
        const intersectsViewport =
          left + scaledSize > -32 &&
          left < w + 32 &&
          top + scaledSize > -32 &&
          top < h + 32;

        if (intersectsViewport) {
          if (entry.img.parentNode !== this._backdropPane) {
            this._backdropPane.appendChild(entry.img);
          }
          entry.img.style.display = 'block';
          entry.img.style.opacity = '1';
          entry.img.style.width = `${Math.ceil(scaledSize + 0.5)}px`;
          entry.img.style.height = `${Math.ceil(scaledSize + 0.5)}px`;
          entry.img.style.transform = `translate3d(${Math.floor(left)}px, ${Math.floor(top)}px, 0)`;
          continue;
        }
      }

      // Hide non-active loaded tile but keep in LRU cache so navigating back is instant (0ms)
      entry.img.style.display = 'none';
    }

    // Cap LRU cache size to 160 loaded tiles
    if (this._tileCache.size > 160) {
      const inactive = Array.from(this._tileCache.values())
        .filter(e => !activeKeys.has(e.key))
        .sort((a, b) => a.lastUsed - b.lastUsed);
      const toRemove = this._tileCache.size - 160;
      for (let i = 0; i < Math.min(toRemove, inactive.length); i++) {
        inactive[i].img.remove();
        this._tileCache.delete(inactive[i].key);
      }
    }
  }

  _renderPolylines() {
    while (this._svgOverlay.firstChild) this._svgOverlay.removeChild(this._svgOverlay.firstChild);
    for (const poly of this._polylines) {
      if (!poly._map || !poly._path?.length) continue;
      const pts = poly._path.map(p => {
        const lat = typeof p.lat === 'function' ? p.lat() : p.lat;
        const lng = typeof p.lng === 'function' ? p.lng() : p.lng;
        const px = this._latLngToContainerPixel(lat, lng);
        return `${px.x.toFixed(1)},${px.y.toFixed(1)}`;
      }).join(' ');
      const el = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
      el.setAttribute('points', pts);
      el.setAttribute('fill', 'none');
      el.setAttribute('stroke', poly._strokeColor || '#b92d49');
      el.setAttribute('stroke-opacity', String(poly._strokeOpacity ?? 0.9));
      el.setAttribute('stroke-width', String(poly._strokeWeight ?? 5));
      el.setAttribute('stroke-linecap', 'round');
      el.setAttribute('stroke-linejoin', 'round');
      if (poly._dashed) {
        const glow = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
        glow.setAttribute('points', pts);
        glow.setAttribute('fill', 'none');
        glow.setAttribute('stroke', poly._strokeColor || '#b92d49');
        glow.setAttribute('stroke-opacity', '0.35');
        glow.setAttribute('stroke-width', String((poly._strokeWeight ?? 2) + 4));
        glow.setAttribute('stroke-linecap', 'round');
        this._svgOverlay.appendChild(glow);
        el.setAttribute('class', 'interconnect-cable');
        el.setAttribute('stroke-dasharray', '1.6 7');
      }
      this._svgOverlay.appendChild(el);
    }
  }

  _renderMarkers() {
    const w = this._container.clientWidth || 960;
    const h = this._container.clientHeight || 640;
    const scale = 2 ** this._zoom;
    const worldWidth = TILE_SIZE * scale;
    const cWorld = latLngToWorldPoint(this._center.lat(), this._center.lng());
    for (const marker of this._markers) {
      marker._updateDOM(this, w, h, scale, worldWidth, cWorld);
    }
  }

  _renderInfoWindows() {
    for (const iw of this._infoWindows) {
      iw._updateDOM(this);
    }
  }
}

class Marker extends Emitter {
  constructor(opts = {}) {
    super();
    this._position = opts.position || { lat: 0, lng: 0 };
    this._worldPoint = latLngToWorldPoint(this._position.lat, this._position.lng);
    this._title = opts.title || '';
    this._icon = opts.icon || null;
    this._label = opts.label || null;
    this._visible = opts.visible !== false;
    this._zIndex = opts.zIndex ?? 10;
    this._map = null;
    this._mounted = false;
    this._appliedIconUrl = '';
    this._appliedW = 0;
    this._appliedH = 0;
    this._appliedLabelText = '';
    this._appliedZ = -1;

    this._el = document.createElement('div');
    this._el.dataset.cherryInteractive = 'true';
    this._el.dataset.cherryMarker = 'true';
    this._el._cherryMarker = this;
    this._el.style.cssText = 'position:absolute;left:0;top:0;pointer-events:auto;cursor:pointer;touch-action:none;will-change:transform;';

    this._img = document.createElement('img');
    this._img.alt = '';
    this._img.draggable = false;
    this._img.style.cssText = 'display:block;user-select:none;pointer-events:auto;';

    this._labelEl = document.createElement('div');
    this._labelEl.dataset.cherryInteractive = 'true';
    this._labelEl.dataset.cherryMarker = 'true';
    this._labelEl._cherryMarker = this;
    this._labelEl.style.cssText = 'position:absolute;pointer-events:auto;cursor:pointer;transform:translate(-50%, -50%);display:none;white-space:nowrap;';

    this._el.append(this._img, this._labelEl);

    this._el.addEventListener('mouseenter', e => this._emit('mouseover', e));
    this._el.addEventListener('mouseleave', e => this._emit('mouseout', e));

    if (opts.map) this.setMap(opts.map);
  }

  setMap(map) {
    if (this._map === map) return;
    if (this._map) {
      this._map._markers.delete(this);
      if (this._mounted) {
        this._el.remove();
        this._mounted = false;
      }
    }
    this._map = map;
    if (map) {
      map._markers.add(this);
      map._scheduleRender();
    }
  }

  getPosition() {
    return new LatLng(this._position.lat, this._position.lng);
  }

  setPosition(pos) {
    const lat = typeof pos.lat === 'function' ? pos.lat() : Number(pos.lat);
    const lng = typeof pos.lng === 'function' ? pos.lng() : Number(pos.lng);
    if (this._position.lat === lat && this._position.lng === lng) return;
    this._position = { lat, lng };
    this._worldPoint = latLngToWorldPoint(lat, lng);
    if (this._map) {
      this._map._scheduleRender();
    }
  }

  getTitle() { return this._title; }
  setTitle(title) {
    const next = title || '';
    if (this._title === next) return;
    this._title = next;
    this._el.title = this._title;
  }

  setVisible(visible) {
    const next = Boolean(visible);
    if (this._visible === next) return;
    this._visible = next;
    if (this._map) this._map._scheduleRender();
  }

  setIcon(icon) {
    if (this._icon?.url === icon?.url && this._icon?.scaledSize?.width === icon?.scaledSize?.width) return;
    this._icon = icon;
    if (this._map) this._map._scheduleRender();
  }

  setZIndex(zIndex) {
    const next = Number(zIndex) || 10;
    if (this._zIndex === next) return;
    this._zIndex = next;
    if (this._mounted) this._el.style.zIndex = String(this._zIndex);
  }

  setLabel(label) {
    const prevText = this._label?.text || '';
    const nextText = label?.text || '';
    if (prevText === nextText && this._label?.color === label?.color) return;
    this._label = label;
    if (this._map) this._map._scheduleRender();
  }

  _updateDOM(map, viewW, viewH, scale, worldWidth, cWorld) {
    if (!this._visible) {
      if (this._mounted) {
        this._el.remove();
        this._mounted = false;
      }
      return;
    }
    const pWorld = this._worldPoint;
    let dx = (pWorld.x - cWorld.x) * scale;
    dx = ((dx + worldWidth / 2) % worldWidth + worldWidth) % worldWidth - worldWidth / 2;
    const dy = (pWorld.y - cWorld.y) * scale;
    const pxX = viewW / 2 + dx;
    const pxY = viewH / 2 + dy;

    if (pxX < -64 || pxX > viewW + 64 || pxY < -64 || pxY > viewH + 64) {
      if (this._mounted) {
        this._el.remove();
        this._mounted = false;
      }
      return;
    }

    if (!this._mounted) {
      map._markerPane.appendChild(this._el);
      this._mounted = true;
    }

    if (this._appliedZ !== this._zIndex) {
      this._el.style.zIndex = String(this._zIndex);
      this._appliedZ = this._zIndex;
    }
    if (this._el.title !== this._title) {
      this._el.title = this._title;
    }

    const icon = this._icon;
    const w = icon?.scaledSize?.width ?? 28;
    const h = icon?.scaledSize?.height ?? 34;
    const anchorX = icon?.anchor?.x ?? w / 2;
    const anchorY = icon?.anchor?.y ?? h;

    if (icon?.url && this._appliedIconUrl !== icon.url) {
      this._img.src = icon.url;
      this._appliedIconUrl = icon.url;
    }
    if (this._appliedW !== w || this._appliedH !== h) {
      this._img.style.width = `${w}px`;
      this._img.style.height = `${h}px`;
      this._appliedW = w;
      this._appliedH = h;
    }

    this._el.style.transform = `translate3d(${Math.round(pxX - anchorX)}px, ${Math.round(pxY - anchorY)}px, 0)`;

    const nextLabelText = this._label?.text || '';
    if (nextLabelText) {
      if (this._appliedLabelText !== nextLabelText) {
        const originX = icon?.labelOrigin?.x ?? w / 2;
        const originY = icon?.labelOrigin?.y ?? h + 10;
        this._labelEl.style.display = 'block';
        this._labelEl.className = this._label.className || '';
        this._labelEl.textContent = nextLabelText;
        this._labelEl.style.color = this._label.color || '#252d2b';
        this._labelEl.style.fontFamily = this._label.fontFamily || 'Manrope, sans-serif';
        this._labelEl.style.fontSize = this._label.fontSize || '11px';
        this._labelEl.style.fontWeight = this._label.fontWeight || '700';
        this._labelEl.style.left = `${originX}px`;
        this._labelEl.style.top = `${originY}px`;
        this._appliedLabelText = nextLabelText;
      }
    } else if (this._appliedLabelText !== '') {
      this._labelEl.style.display = 'none';
      this._appliedLabelText = '';
    }
  }
}

class InfoWindow extends Emitter {
  constructor(opts = {}) {
    super();
    this._opts = opts;
    this._content = null;
    this._map = null;
    this._anchor = null;

    this._el = document.createElement('div');
    this._el.dataset.cherryInteractive = 'true';
    this._el.style.cssText = 'position:absolute;left:0;top:0;transform:translate(-50%, -100%);background:#fffefa;border:1px solid #e7ece7;border-radius:14px;box-shadow:0 8px 36px #263b3424;padding:12px 14px 8px;pointer-events:auto;display:none;z-index:2000;';

    this._closeBtn = document.createElement('button');
    this._closeBtn.type = 'button';
    this._closeBtn.setAttribute('aria-label', 'Close dialog');
    this._closeBtn.textContent = '×';
    this._closeBtn.style.cssText = 'position:absolute;top:6px;right:8px;width:22px;height:22px;border-radius:6px;background:transparent;color:#7d897d;font-size:16px;line-height:1;display:flex;align-items:center;justify-content:center;';
    this._closeBtn.addEventListener('click', e => {
      e.stopPropagation();
      this.close();
      this._emit('closeclick');
    });

    this._body = document.createElement('div');
    this._el.append(this._closeBtn, this._body);
  }

  setContent(content) {
    this._content = content;
    this._body.innerHTML = '';
    if (typeof content === 'string') {
      this._body.innerHTML = content;
    } else if (content instanceof Node) {
      this._body.appendChild(content);
    }
    const isTooltip = this._opts.disableAutoPan && !(content instanceof HTMLElement && content.classList.contains('marker-popup'));
    this._closeBtn.style.display = isTooltip ? 'none' : 'flex';
    this._el.style.pointerEvents = isTooltip ? 'none' : 'auto';
    if (this._map) this._updateDOM(this._map);
  }

  setOptions(opts = {}) {
    this._opts = { ...this._opts, ...opts };
  }

  open(config = {}) {
    const map = config.map || this._map;
    const anchor = config.anchor || this._anchor;
    if (!map) return;
    if (this._map && this._map !== map) {
      this._map._infoWindows.delete(this);
      this._el.remove();
    }
    this._map = map;
    this._anchor = anchor;
    map._infoWindows.add(this);
    if (!this._el.parentNode) map._infoPane.appendChild(this._el);
    this._el.style.display = 'block';
    this._updateDOM(map);
  }

  close() {
    this._el.style.display = 'none';
    if (this._map) {
      this._map._infoWindows.delete(this);
    }
  }

  _updateDOM(map) {
    if (this._el.style.display === 'none' || !this._anchor) return;
    const pos = this._anchor.getPosition();
    const px = map._latLngToContainerPixel(pos.lat(), pos.lng());
    const iconH = this._anchor._icon?.scaledSize?.height ?? 32;
    this._el.style.left = `${Math.round(px.x)}px`;
    this._el.style.top = `${Math.round(px.y - iconH - 6)}px`;
  }
}

class Polyline extends Emitter {
  constructor(opts = {}) {
    super();
    this._path = opts.path || [];
    this._strokeColor = opts.strokeColor || '#b92d49';
    this._strokeOpacity = opts.strokeOpacity ?? 0.9;
    this._strokeWeight = opts.strokeWeight ?? 5;
    this._dashed = !!opts.dashed;
    this._map = null;
    if (opts.map) this.setMap(opts.map);
  }

  setMap(map) {
    if (this._map === map) return;
    if (this._map) {
      this._map._polylines.delete(this);
      this._map._renderPolylines();
    }
    this._map = map;
    if (map) {
      map._polylines.add(this);
      map._renderPolylines();
    }
  }
}

export function ensureGoogleMapsEngine() {
  if (typeof window === 'undefined') return;
  if (window.google?.maps?.Map === CustomGoogleMap) return;
  window.google = window.google || {};
  window.google.maps = {
    Map: CustomGoogleMap,
    Marker,
    InfoWindow,
    Polyline,
    LatLng,
    LatLngBounds,
    Size,
    Point,
    event: {
      clearInstanceListeners: instance => {
        if (instance && typeof instance._clearListeners === 'function') {
          instance._clearListeners();
        }
      },
    },
  };
}