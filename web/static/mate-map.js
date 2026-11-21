/* China map tiles/styles adapted from ha-leapmotor-cn/src/lib/geo.ts and
 * src/cards/map.ts (AGPLv3). Leaflet's public coordinates remain WGS84:
 * only projection changes to GCJ02, including the inverse for clicks/drags.
 */
(function (root) {
  'use strict'
  const sources = Object.freeze({
    road: 'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
    satellite: 'https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}',
    osm: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'
  })
  const config = document.documentElement.dataset
  const useAmap = config.mateMapProvider === 'amap'
  const vectorKey = useAmap && config.mateAmapJsKey
  const styles = useAmap ? ['auto', 'vector', 'satellite', 'dark'] : ['auto', 'vector', 'dark']
  const names = {auto: '自动', vector: '路网', satellite: '卫星', dark: '深色'}
  const maps = new Set()
  let selected = 'auto'
  try {
    const saved = localStorage.getItem('mate.map.style')
    if (styles.includes(saved)) selected = saved
  } catch (_) {}

  function effectiveStyle(style) {
    if (style !== 'auto') return style
    const rgb = getComputedStyle(document.body).backgroundColor.match(/[\d.]+/g)
    return rgb && Number(rgb[0]) + Number(rgb[1]) + Number(rgb[2]) < 384 ? 'dark' : 'vector'
  }

  function crs(L) {
    const mercator = L.Projection.SphericalMercator
    const coordinates = root.MateCoordinates
    return L.extend({}, L.CRS.EPSG3857, {
      code: 'MATE:WGS84-GCJ02',
      projection: {
        bounds: mercator.bounds,
        project(at) {
          const pair = coordinates.wgs84ToGcj02(at.lat, at.lng)
          return mercator.project(L.latLng(pair[0], pair[1]))
        },
        unproject(point) {
          const at = mercator.unproject(point)
          const pair = coordinates.gcj02ToWgs84(at.lat, at.lng)
          return L.latLng(pair[0], pair[1])
        }
      }
    })
  }

  function create(element, options) {
    const L = root.L
    const basemapOptions = {crs: useAmap ? crs(L) : L.CRS.EPSG3857, maxZoom: 18}
    if (vectorKey) Object.assign(basemapOptions, {minZoom: 2, zoomAnimation: false})
    const map = L.map(element, Object.assign({}, options, basemapOptions))
    if (map.attributionControl) map.attributionControl.setPrefix(false)
    map.mateCoordinateSystem = 'WGS84'
    return map
  }

  let sdk
  function loadVectorSdk() {
    if (!sdk) {
      sdk = new Promise(function (resolve, reject) {
        const script = document.createElement('script')
        const timeout = root.setTimeout(fail, 10000)
        function fail() {
          root.clearTimeout(timeout)
          script.remove()
          reject(new Error('AMap SDK unavailable'))
        }
        root._AMapSecurityConfig = {serviceHost: new URL('api/_AMapService', document.baseURI).href}
        script.src = 'https://webapi.amap.com/maps?v=2.0&key=' + encodeURIComponent(vectorKey)
        script.onerror = fail
        script.onload = function () {
          if (!root.AMap || !root.AMap.Map) return fail()
          root.clearTimeout(timeout)
          resolve(root.AMap)
        }
        document.head.appendChild(script)
      })
    }
    return sdk
  }

  // Leaflet owns the viewport and overlays. The SDK renders only the GCJ02
  // basemap in a separate container, so markers, click events and storage stay WGS84.
  function vectorBasemap(map, style, onReady, onFailure) {
    const container = document.createElement('div')
    container.className = 'mate-amap-vector'
    container.setAttribute('aria-hidden', 'true')
    container.style.visibility = 'hidden'
    map.getContainer().prepend(container)
    const mapStyle = () => 'amap://styles/' + (style === 'dark' ? 'dark' : 'normal')
    function center() {
      const at = map.getCenter()
      const [lat, lng] = root.MateCoordinates.wgs84ToGcj02(at.lat, at.lng)
      return [lng, lat]
    }
    let active = true
    let vector
    let frame
    const timeout = root.setTimeout(fail, 15000)
    function sync() {
      if (!vector || frame !== undefined) return
      frame = root.requestAnimationFrame(function () {
        frame = undefined
        if (active) vector.setZoomAndCenter(map.getZoom(), center(), true)
      })
    }
    function destroy() {
      active = false
      root.clearTimeout(timeout)
      root.cancelAnimationFrame(frame)
      map.off('move zoom resize', sync)
      if (vector) vector.destroy()
      container.remove()
    }
    function fail() {
      if (!active) return
      destroy()
      onFailure()
    }
    loadVectorSdk().then(function (AMap) {
      if (!active) return
      vector = new AMap.Map(container, {
        viewMode: '2D', mapStyle: mapStyle(), center: center(), zoom: map.getZoom(),
        // Leaflet handles gestures. Keep SDK zoom enabled for setZoomAndCenter.
        zooms: [2, 18], resizeEnable: true, zoomEnable: true, animateEnable: false,
        dragEnable: false, keyboardEnable: false, scrollWheel: false,
        doubleClickZoom: false, touchZoom: false
      })
      vector.on('complete', function () {
        if (!active || container.style.visibility === 'visible') return
        root.clearTimeout(timeout)
        container.style.visibility = 'visible'
        onReady()
      })
      vector.on('error', fail)
      map.on('move zoom resize', sync)
      sync()
    }).catch(fail)
    return {
      destroy,
      setStyle(nextStyle) {
        if (style === nextStyle) return
        style = nextStyle
        if (vector) vector.setMapStyle(mapStyle())
      }
    }
  }

  function addTiles(map) {
    const L = root.L
    if (map._mateTiles) return map._mateTiles
    const state = {layer: null, control: null, select: null, notice: null, style: selected,
      fallback: false, standard: false, vector: null, vectorReady: false, vectorFailed: false}
    map._mateTiles = state
    maps.add(map)

    if (!document.getElementById('mate-map-css')) {
      const css = document.createElement('style')
      css.id = 'mate-map-css'
      css.textContent = '.mate-map-dark{filter:invert(100%) hue-rotate(180deg) brightness(85%) contrast(90%)}' +
        '.mate-map-style{background:#0f172a;color:#e2e8f0;border:1px solid #475569;border-radius:6px;padding:4px;max-width:170px;font:12px sans-serif}' +
        '.mate-map-style select{background:#0f172a;color:#e2e8f0;border:0;padding:4px;max-width:100%}' +
        '.mate-map-style small:not([hidden]){display:block;max-width:155px;font-size:10px;padding:2px;white-space:normal}' +
        '.leaflet-control-attribution:empty{display:none}' +
        '.mate-amap-vector{position:absolute;inset:0;z-index:0;pointer-events:none}'
      document.head.appendChild(css)
    }

    function apply() {
      const center = map._loaded ? map.getCenter() : null
      const zoom = map._loaded ? map.getZoom() : null
      if (state.layer) map.removeLayer(state.layer)
      state.layer = null
      const style = effectiveStyle(state.style)
      if (vectorKey && center && style !== 'satellite' && !state.vectorFailed) {
        if (state.vector) state.vector.setStyle(style)
        else state.vector = vectorBasemap(map, style, function () {
          state.vectorReady = true
          apply()
        }, function () {
          state.vector = null
          state.vectorReady = false
          state.vectorFailed = true
          apply()
        })
      } else if (state.vector) {
        state.vector.destroy()
        state.vector = null
        state.vectorReady = false
      }
      // A fallback must switch the whole map to WGS84; mixing OSM tiles into
      // a GCJ02 grid would reintroduce the offset this adapter fixes.
      const amapLayer = useAmap && (!state.fallback || state.vectorReady)
      map.options.crs = amapLayer ? crs(L) : L.CRS.EPSG3857
      map.mateTileCoordinateSystem = amapLayer ? 'GCJ02' : 'WGS84'
      if (!state.vectorReady) addRaster(amapLayer, style)
      if (center) map.setView(center, zoom, {reset: true, animate: false})
      if (state.select) state.select.value = state.style
      if (state.notice) {
        state.notice.textContent = state.vectorReady ? ''
          : state.fallback ? '高德暂不可用，已切换 OpenStreetMap'
          : state.vectorFailed && style !== 'satellite' ? config.mateAmapVectorFallback
          : state.standard ? '高清路网暂不可用，已切换标准路网' : ''
        state.notice.hidden = !state.notice.textContent
      }
    }

    function addRaster(amapLayer, style) {
      const url = amapLayer ? sources[style === 'satellite' ? 'satellite' : 'road'] : sources.osm
      const highDpi = L.Browser.retina && amapLayer && !state.standard && style !== 'satellite'
      const layer = L.tileLayer(url, {
        subdomains: '1234', maxZoom: highDpi ? 19 : 18, referrerPolicy: 'origin',
        detectRetina: highDpi, maxNativeZoom: highDpi ? 17 : 18,
        attribution: amapLayer ? '© 高德地图' : '© OpenStreetMap contributors',
        className: style === 'dark' ? 'mate-map-dark' : ''
      })
      state.layer = layer
      let failures = 0
      layer.on('tileerror', function () {
        if (state.layer !== layer || !amapLayer || ++failures < 3) return
        if (highDpi) state.standard = true
        else state.fallback = true
        apply()
      })
      layer.addTo(map)
    }

    const StyleControl = L.Control.extend({
      options: {position: 'topright'},
      onAdd() {
        const box = L.DomUtil.create('div', 'mate-map-style')
        const select = document.createElement('select')
        select.setAttribute('aria-label', '地图底图')
        for (const style of styles) {
          const option = document.createElement('option')
          option.value = style
          option.textContent = names[style]
          select.appendChild(option)
        }
        const notice = document.createElement('small')
        notice.setAttribute('role', 'status')
        notice.hidden = true
        box.append(select, notice)
        state.select = select
        state.notice = notice
        select.value = state.style
        L.DomEvent.disableClickPropagation(box)
        L.DomEvent.disableScrollPropagation(box)
        select.addEventListener('change', function () {
          selected = select.value
          try { localStorage.setItem('mate.map.style', selected) } catch (_) {}
          for (const active of maps) active._mateTiles.setStyle(selected)
        })
        return box
      }
    })
    state.setStyle = function (style) {
      state.style = styles.includes(style) ? style : 'auto'
      state.fallback = false
      state.standard = false
      apply()
    }
    state.control = new StyleControl().addTo(map)
    apply()
    const media = root.matchMedia('(prefers-color-scheme: dark)')
    const themeChanged = function () { if (state.style === 'auto') apply() }
    media.addEventListener('change', themeChanged)
    map.on('unload', function () {
      if (state.vector) state.vector.destroy()
      media.removeEventListener('change', themeChanged)
      maps.delete(map)
    })
    if (vectorKey && !map._loaded) map.whenReady(apply)
    return state
  }

  root.MateMap = Object.freeze({create, addTiles, sources, defaultCenter: [35.8617, 104.1954]})
})(globalThis)
