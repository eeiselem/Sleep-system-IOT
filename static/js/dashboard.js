    function parseUtcTimestamp(timestampStr) {
        if (!timestampStr) return null;
        const trimmed = String(timestampStr).trim();
        if (!trimmed) return null;

        // If server provides naive timestamp, treat it as UTC.
        if (
            !trimmed.endsWith('Z') &&
            !/[+-]\d{2}:\d{2}$/.test(trimmed)
        ) {
            return new Date(`${trimmed}Z`);
        }
        return new Date(trimmed);
    }

    /** IANA zone for single-night charts (browser local, so labels match the viewer’s clock). */
    const SLEEP_CHART_TZ =
        typeof Intl !== 'undefined' && typeof Intl.DateTimeFormat === 'function'
            ? (Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC')
            : 'UTC';

    function formatLocalClock(ms) {
        const d = new Date(ms);
        if (Number.isNaN(d.getTime())) return '';
        return new Intl.DateTimeFormat('en-US', {
            timeZone: SLEEP_CHART_TZ,
            hour: 'numeric',
            minute: '2-digit',
            hour12: true,
            timeZoneName: 'short',
        }).format(d);
    }

    function sleepNightTooltipTitleLocal(items) {
        const it = items && items[0];
        if (!it || it.parsed.x === undefined || it.parsed.x === null) return '';
        const d = new Date(it.parsed.x);
        if (Number.isNaN(d.getTime())) return '';
        return new Intl.DateTimeFormat('en-US', {
            timeZone: SLEEP_CHART_TZ,
            weekday: 'short',
            month: 'short',
            day: 'numeric',
            year: 'numeric',
            hour: 'numeric',
            minute: '2-digit',
            second: '2-digit',
            hour12: true,
            timeZoneName: 'long',
        }).format(d);
    }

    /** Keep in sync with ``utils.format_restlessness_band_from_score`` (0 to 100 restful efficiency). */
    function formatRestlessnessJs(score) {
        if (!Number.isFinite(score)) return '-';
        if (score >= 80) return 'Excellent (still / restful)';
        if (score >= 50) return 'Moderate rest efficiency';
        return 'Low rest efficiency (restless)';
    }

    // Setup live clock
    setInterval(() => {
        document.getElementById('live-clock').innerText = new Date().toLocaleTimeString();
    }, 1000);

    /* Generated from templates/dashboard.html - boot from inline snippet in template. */
const LIVE_READINGS_INITIAL = window.__DASH_BOOT__.live;
    let liveReadingsCache = LIVE_READINGS_INITIAL;

    /** Sleep-session charts (single night); weekly view uses one multi-series line chart; sleep score history is its own line chart. */
    let sleepScoreHistoryChart = null;
    let sleepWeeklyMultiChart = null;
    let sleepNightCharts = [];

    /** Latest `/api/simulated-room` sunrise block; drives wake-time fallback when the time input is empty. */
    let lastSunriseSequence = null;

    /** Last wake time persisted from the server (``cfg-wake-time`` is draft until Save). */
    let savedCfgWakeTime = '';

    let sunriseDemoActive = false;
    let sunriseDemoRafId = null;

    /**
     * Minutes until the next occurrence of HH:MM (24h clock) today in local time, else tomorrow if past.
     */
    function computeMinutesToWakeFromWakeClockHm(wakeHM) {
        if (!wakeHM || typeof wakeHM !== 'string') return null;
        const trimmed = wakeHM.trim();
        const parts = trimmed.split(':');
        if (parts.length < 2) return null;
        const h = parseInt(parts[0], 10);
        const mm = parseInt(parts[1], 10);
        if (!Number.isFinite(h) || !Number.isFinite(mm) || h < 0 || h > 23 || mm < 0 || mm > 59) {
            return null;
        }
        const now = new Date();
        const wakeTarget = new Date(now.getFullYear(), now.getMonth(), now.getDate(), h, mm, 0, 0);
        let deltaMs = wakeTarget.getTime() - Date.now();
        if (deltaMs < 0) deltaMs += 86400000;
        return deltaMs / 60000;
    }

    /** Same wake-clock source as Minutes-to-Wake display (config input → API wake_time). */
    function getUnifiedMinutesToWake() {
        const wakeEl = document.getElementById('cfg-wake-time');
        let wakeHM = wakeEl && wakeEl.value ? String(wakeEl.value).trim() : '';
        if (!wakeHM && lastSunriseSequence && lastSunriseSequence.wake_time) {
            wakeHM = String(lastSunriseSequence.wake_time).trim();
        }
        return computeMinutesToWakeFromWakeClockHm(wakeHM);
    }

    function updateMinutesToWakeDisplay() {
        if (sunriseDemoActive) return;

        const minEl = document.getElementById('sunrise-minutes');
        const mins = getUnifiedMinutesToWake();
        if (minEl) minEl.innerText = mins !== null ? mins.toFixed(1) : '-';
    }

    /**
     * Real-time smart-bulb prelude for the next 30 local minutes before wake:
     * background #000 → daylight via lumen/Kelvin map; lumens forced to 0 when >30 min out.
     */
    function applyLocalSunrisePreludeFromMinutes(mins) {
        if (!Number.isFinite(mins)) return;

        const { kLo } = getSunriseRanges();

        if (mins > 30) {
            applySunriseVisual(0, 0, kLo);
            const st = document.getElementById('sunrise-status');
            if (st) {
                st.innerText =
                    '>30 min to wake · bulb off (#000)';
            }
            return;
        }

        const progressDecimal = mins <= 0 ? 1 : (30 - mins) / 30;
        const progressPct = Math.min(Math.max(progressDecimal * 100, 0), 100);
        const { lumen, kelvin } = mapProgressToLumenKelvin(progressPct);
        applySunriseVisual(progressPct, lumen, kelvin);

        const st = document.getElementById('sunrise-status');
        if (st) {
            if (mins <= 0) {
                st.innerText =
                    `Wake · daylight cue (${progressPct.toFixed(1)}% ramp)`;
            } else {
                st.innerText =
                    `Sunrise prelude (${progressPct.toFixed(1)}% · ${mins.toFixed(1)} min to wake)`;
            }
        }
    }

    function tickSunriseRealtimePrelude() {
        if (sunriseDemoActive) return;
        updateMinutesToWakeDisplay();
        const mins = getUnifiedMinutesToWake();
        if (mins !== null && Number.isFinite(mins)) {
            applyLocalSunrisePreludeFromMinutes(mins);
        }
    }

    const LIVE_STALE_MS = 2 * 60 * 1000;
    const LIVE_DELAY_MS = 5 * 60 * 1000;
    const LIVE_OFFLINE_MS = 30 * 60 * 1000;

    const LIVE_METRIC_AGE_TO_PRIMARY = {
        'live-age-temperature': () => document.querySelector('.temp-val'),
        'live-age-humidity': () => document.querySelector('.hum-val'),
        'live-age-heart-rate': () => document.getElementById('live-heart-rate'),
        'live-age-spo2': () => document.getElementById('live-spo2'),
        'live-age-ambient-noise': () => document.getElementById('live-ambient-noise'),
        'live-age-voc': () => document.getElementById('live-voc'),
        'live-age-lumens': () => document.getElementById('live-lumens'),
        'live-age-restlessness': () => document.getElementById('live-restlessness'),
    };

    function clearAllLivePrimaryOfflineClasses() {
        Object.keys(LIVE_METRIC_AGE_TO_PRIMARY).forEach((k) => {
            const el = LIVE_METRIC_AGE_TO_PRIMARY[k]();
            if (el) el.classList.remove('live-metric-primary--offline');
        });
    }

    function liveMetricHasReadableValue(val) {
        if (val === null || val === undefined) return false;
        const s = String(val).trim();
        if (!s || s.toUpperCase() === 'N/A') return false;
        return true;
    }

    function liveReadingsLooksEmpty(data) {
        if (!data || typeof data !== 'object') return true;
        const ts = data.timestamp != null && String(data.timestamp).trim() !== '';
        const keys = [
            'temperature',
            'humidity',
            'heart_rate',
            'spo2',
            'ambient_noise',
            'air_quality',
            'ambient_light',
            'restlessness_score',
        ];
        const anyVal = keys.some((k) => liveMetricHasReadableValue(data[k]));
        return !ts && !anyVal;
    }

    /** Display values from `/api/latest-readings` (matches Reading model fields). */
    function formatLiveReadingMetric(val, unitSuffix) {
        if (val === null || val === undefined || val === 'N/A') return '-';
        const s = String(val).trim();
        if (!s || s.toUpperCase() === 'N/A') return '-';
        if (/error/i.test(s)) {
            const sl = s.toLowerCase();
            if (
                sl.includes('decod')
                || sl.includes('padding')
                || sl.includes('decrypt')
                || sl.includes('base64')
            ) {
                return 'Encrypting…';
            }
            return '-';
        }
        const n = Number(s);
        if (!Number.isFinite(n)) return s;
        if (unitSuffix === '%' || unitSuffix === 'bpm') {
            return Number.isInteger(n) ? String(Math.round(n)) : String(Number(n.toFixed(1)));
        }
        return Number.isInteger(n) ? String(Math.round(n)) : String(Number(n.toFixed(2)));
    }

    /** Live API temperature is stored as °C; dashboard shows °F. */
    function formatLiveTemperatureFahrenheit(val) {
        if (val === null || val === undefined || val === 'N/A') return '-';
        const s = String(val).trim();
        if (!s || s.toUpperCase() === 'N/A') return '-';
        if (/error/i.test(s)) {
            const sl = s.toLowerCase();
            if (
                sl.includes('decod')
                || sl.includes('padding')
                || sl.includes('decrypt')
                || sl.includes('base64')
            ) {
                return 'Encrypting…';
            }
            return '-';
        }
        const c = Number(s);
        if (!Number.isFinite(c)) return s;
        const f = c * (9 / 5) + 32;
        return String(Number(f.toFixed(1)));
    }

    const DASH_TEMP_TIER_CLASS = {
        neutral: 'temp-val dash-temp-tier-neutral',
        ok: 'temp-val dash-temp-tier-ok',
        amber: 'temp-val dash-temp-tier-amber',
        red: 'temp-val dash-temp-tier-red',
    };

    function formatMetricAgeLine(iso, hasValue) {
        if (!hasValue) {
            return { text: '-', title: 'No samples yet', delayed: false, offline: false };
        }
        if (!iso) {
            return { text: '-', title: '', delayed: false, offline: false };
        }
        const d = parseUtcTimestamp(iso);
        if (!d || Number.isNaN(d.getTime())) {
            return { text: '-', title: '', delayed: false, offline: false };
        }
        const now = Date.now();
        const delta = Math.max(0, now - d.getTime());
        const title = d.toLocaleString();
        if (delta < 45_000) {
            const sec = Math.floor(delta / 1000);
            return { text: sec < 10 ? 'just now' : `${sec}s ago`, title, delayed: false, offline: false };
        }
        if (delta < LIVE_STALE_MS) {
            const sec = Math.floor(delta / 1000);
            const m = Math.floor(sec / 60);
            return { text: `${m}m ${sec % 60}s ago`, title, delayed: false, offline: false };
        }
        if (delta < LIVE_DELAY_MS) {
            const m = Math.floor(delta / 60000);
            return { text: `${m}m ago · catching up`, title, delayed: false, offline: false };
        }
        if (delta >= LIVE_OFFLINE_MS) {
            return { text: 'Offline', title, delayed: false, offline: true };
        }
        const m = Math.floor(delta / 60000);
        const h = Math.floor(delta / 3600000);
        const label = h >= 1 ? `${h}h ${m % 60}m ago` : `${m}m ago`;
        return { text: `${label} · delayed`, title, delayed: true, offline: false };
    }

    function setMetricAgeElement(id, iso, val) {
        const el = document.getElementById(id);
        if (!el) return;
        const has = liveMetricHasReadableValue(val);
        const { text, title, delayed, offline } = formatMetricAgeLine(iso, has);
        el.textContent = text;
        el.title = title;
        el.classList.toggle('live-metric-age--delayed', delayed && !offline);
        el.classList.toggle('live-metric-age--offline', !!offline);
        const primGet = LIVE_METRIC_AGE_TO_PRIMARY[id];
        if (primGet) {
            const p = primGet();
            if (p) p.classList.toggle('live-metric-primary--offline', !!offline);
        }
    }

    function setLiveReadingsNoDataState() {
        clearAllLivePrimaryOfflineClasses();
        const t = document.querySelector('.temp-val');
        const h = document.querySelector('.hum-val');
        if (t) {
            t.innerText = '-';
            t.className = DASH_TEMP_TIER_CLASS.neutral;
        }
        if (h) h.innerText = '-';
        ['live-heart-rate', 'live-spo2', 'live-ambient-noise', 'live-voc', 'live-lumens', 'live-restlessness'].forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.innerText = '-';
        });
        [
            'live-age-temperature',
            'live-age-humidity',
            'live-age-heart-rate',
            'live-age-spo2',
            'live-age-ambient-noise',
            'live-age-voc',
            'live-age-lumens',
            'live-age-restlessness',
        ].forEach((id) => {
            const el = document.getElementById(id);
            if (el) {
                el.textContent = '-';
                el.title = 'No samples yet';
                el.classList.remove('live-metric-age--delayed', 'live-metric-age--offline');
            }
        });
    }

    function applyLiveReadingsPayload(data) {
        if (!data || typeof data !== 'object') return;
        liveReadingsCache = data;
        if (liveReadingsLooksEmpty(data)) {
            setLiveReadingsNoDataState();
            return;
        }

        const tEl = document.querySelector('.temp-val');
        const hEl = document.querySelector('.hum-val');
        if (tEl) tEl.innerText = formatLiveTemperatureFahrenheit(data.temperature);
        if (hEl) hEl.innerText = formatLiveReadingMetric(data.humidity, '%');

        const hr = document.getElementById('live-heart-rate');
        const sp = document.getElementById('live-spo2');
        const nz = document.getElementById('live-ambient-noise');
        const voc = document.getElementById('live-voc');
        const lux = document.getElementById('live-lumens');
        if (hr) hr.innerText = formatLiveReadingMetric(data.heart_rate, 'bpm');
        if (sp) sp.innerText = formatLiveReadingMetric(data.spo2, '%');
        if (nz) nz.innerText = formatLiveReadingMetric(data.ambient_noise, 'dB');
        if (voc) voc.innerText = formatLiveReadingMetric(data.air_quality, '');
        if (lux) lux.innerText = formatLiveReadingMetric(data.ambient_light, '');
        const rl = document.getElementById('live-restlessness');
        if (rl) rl.innerText = formatLiveReadingMetric(data.restlessness_score, '');

        setMetricAgeElement('live-age-temperature', data.temperature_updated_at, data.temperature);
        setMetricAgeElement('live-age-humidity', data.humidity_updated_at, data.humidity);
        setMetricAgeElement('live-age-heart-rate', data.heart_rate_updated_at, data.heart_rate);
        setMetricAgeElement('live-age-spo2', data.spo2_updated_at, data.spo2);
        setMetricAgeElement('live-age-ambient-noise', data.ambient_noise_updated_at, data.ambient_noise);
        setMetricAgeElement('live-age-voc', data.air_quality_updated_at, data.air_quality);
        setMetricAgeElement('live-age-lumens', data.ambient_light_updated_at, data.ambient_light);
        setMetricAgeElement('live-age-restlessness', data.restlessness_score_updated_at, data.restlessness_score);
        updateTemperatureComfortFromCache();
    }

    function destroySleepNightCharts() {
        sleepNightCharts.forEach((c) => {
            try {
                c.destroy();
            } catch (e) { /* noop */ }
        });
        sleepNightCharts = [];
    }

    async function loadSleepSessionsIntoSelectAndPreserveSelection() {
        const sel = document.getElementById('sleep-session-select');
        if (!sel) return;
        const previous = sel.value;
        const res = await fetch('/api/sleep-session/list');
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || res.statusText);
        const sessions = data.sessions || [];
        sel.innerHTML = '';
        sessions.forEach((s) => {
            const opt = document.createElement('option');
            opt.value = String(s.id);
            const badge = s.fsm_active ? ' · live ASLEEP'
                : s.ongoing ? ' · in progress'
                : '';
            const baseLabel = s.display_label
                || `Sleep session ${s.id}`;
            opt.textContent = `${baseLabel}${badge}`;
            sel.appendChild(opt);
        });
        const ids = new Set(sessions.map((s) => String(s.id)));
        if (previous && ids.has(previous)) sel.value = previous;
    }

    function syncNightChartsXAxis(sourceChart) {
        const x = sourceChart.scales.x;
        if (x.min === undefined || x.max === undefined) return;
        sleepNightCharts.forEach((c) => {
            if (c === sourceChart) return;
            c.options.scales.x.min = x.min;
            c.options.scales.x.max = x.max;
            c.update('none');
        });
    }

    function attachSleepNightAxisSync(charts) {
        charts.forEach((chart) => {
            const z = chart.options.plugins.zoom;
            if (!z) return;
            z.pan.onPanComplete = ({ chart: ch }) => syncNightChartsXAxis(ch);
            z.zoom.onZoomComplete = ({ chart: ch }) => syncNightChartsXAxis(ch);
        });
    }

    function mapNightPoints(rows, key) {
        return rows.map((p) => {
            const t = parseUtcTimestamp(p.t);
            const xv = t && !Number.isNaN(t.getTime()) ? t.getTime() : null;
            const raw = p[key];
            let y = null;
            if (raw !== null && raw !== undefined && raw !== '') {
                const num = Number(raw);
                if (Number.isFinite(num)) {
                    y = num;
                }
            }
            const o = { x: xv, y };
            if (key === 'restlessness_score' && p.restlessness) {
                o.restlessness = p.restlessness;
            }
            return o;
        }).filter((pt) => pt.x !== null);
    }

    var SLEEP_ONSET_GUIDE_MSG = 'Data will appear once Sleep Onset is detected.';
    var SLEEP_METRIC_GAP_MSG = 'No samples for this metric in this session window.';

    function setSleepChartGuide(wrapId, active, message) {
        var wrap = document.getElementById(wrapId);
        if (!wrap) return;
        var cap = wrap.querySelector('.sleep-chart-guide-msg');
        wrap.classList.toggle('sleep-chart-guide-on', !!active);
        if (!cap) return;
        if (active) {
            cap.textContent = message || SLEEP_ONSET_GUIDE_MSG;
        } else {
            cap.textContent = '';
        }
    }

    function nightFiniteCount(rows, key) {
        var n = 0;
        for (var i = 0; i < rows.length; i++) {
            var raw = rows[i][key];
            if (raw === null || raw === undefined || raw === '') continue;
            var num = Number(raw);
            if (Number.isFinite(num)) n++;
        }
        return n;
    }

    function ghostSinMetricPoints(tMin, tMax, yLo, yHi, phase, steps) {
        var out = [];
        var span = tMax - tMin;
        var amp = ((yHi - yLo) / 2) * 0.75;
        var mid = (yHi + yLo) / 2;
        var n = typeof steps === 'number' ? steps : 54;
        for (var i = 0; i <= n; i++) {
            var u = i / n;
            var t = tMin + span * u;
            var w = Math.sin(u * Math.PI * 2 + phase) * amp + mid;
            out.push({ x: t, y: +w.toFixed(2) });
        }
        return out;
    }

    async function loadSingleNightCharts() {
        var statusEl = document.getElementById('sleep-single-night-status');
        statusEl.innerText = 'Loading session list…';
        try {
            await loadSleepSessionsIntoSelectAndPreserveSelection();
        } catch (err) {
            console.error(err);
            statusEl.innerText = 'Could not list sessions: ' + err.message;
            return;
        }
        var sel = document.getElementById('sleep-session-select');
        var sessionId = sel && sel.value;
        if (!sessionId) {
            statusEl.innerText = 'No sleep sessions on record yet. Use the ingest path until the onset state enters ASLEEP at least once.';
            destroySleepNightCharts();
            return;
        }

        statusEl.innerText = 'Loading night data…';
        destroySleepNightCharts();

        var payload;
        try {
            var res = await fetch(
                '/api/sleep-session/night-readings?session_id=' + encodeURIComponent(sessionId)
            );
            payload = await res.json();
            if (!res.ok) throw new Error(payload.error || res.statusText);
        } catch (err) {
            console.error(err);
            statusEl.innerText = 'Could not load night: ' + err.message;
            return;
        }

        var rows = payload.points || [];
        var wStart = parseUtcTimestamp(payload.window_start);
        var wEnd = parseUtcTimestamp(payload.window_end);
        if (!wStart || !wEnd || Number.isNaN(wStart.getTime()) || Number.isNaN(wEnd.getTime())) {
            statusEl.innerText = 'Invalid response from server.';
            return;
        }

        var tMin = wStart.getTime();
        var tMax = wEnd.getTime();
        var sessionHasSamples = rows.length > 0;
        var sparse = rows.length > 200;

        if (!sessionHasSamples) {
            statusEl.innerText = 'No samples in this session window yet.';
        } else {
            statusEl.innerText = '';
        }

        /* --- Synchronized per-metric charts --- */
        var detailSpecs = [
            {
                wrapId: 'sleep-wrap-room_temp_f',
                canvasId: 'sleepChartTemp',
                field: 'room_temp_f',
                yTitle: 'Room temperature (°F)',
                color: '#fb923c',
                beginAtZero: false,
                ghostBand: [66, 73],
                phase: 0.08,
            },
            {
                wrapId: 'sleep-wrap-humidity',
                canvasId: 'sleepChartHumidity',
                field: 'humidity',
                yTitle: 'Humidity (%RH)',
                color: '#7dd3fc',
                beginAtZero: true,
                ghostBand: [38, 58],
                phase: 0.28,
            },
            {
                wrapId: 'sleep-wrap-heart_rate',
                canvasId: 'sleepChartHr',
                field: 'heart_rate',
                yTitle: 'Heart rate (bpm)',
                color: '#f4d976',
                beginAtZero: false,
                ghostBand: [58, 86],
                phase: 0.2,
            },
            {
                wrapId: 'sleep-wrap-spo2',
                canvasId: 'sleepChartSpo2',
                field: 'spo2',
                yTitle: 'SpO₂ (%)',
                color: '#2dd4bf',
                spo2Band: true,
                ghostBand: [91, 98],
                phase: 0.9,
            },
            {
                wrapId: 'sleep-wrap-prv_ms',
                canvasId: 'sleepChartPrv',
                field: 'prv_ms',
                yTitle: 'HRV / PRV (ms)',
                color: '#7ef2c5',
                beginAtZero: true,
                ghostBand: [38, 95],
                phase: 1.4,
            },
            {
                wrapId: 'sleep-wrap-ambient_light',
                canvasId: 'sleepChartLight',
                field: 'ambient_light',
                yTitle: 'Ambient light (lux)',
                color: '#fde047',
                beginAtZero: true,
                ghostBand: [12, 220],
                phase: 0.65,
            },
            {
                wrapId: 'sleep-wrap-voc',
                canvasId: 'sleepChartVoc',
                field: 'voc',
                yTitle: 'Air quality index (lower = cleaner)',
                color: '#bef264',
                beginAtZero: true,
                ghostBand: [150, 420],
                phase: 2.0,
            },
            {
                wrapId: 'sleep-wrap-ambient_noise',
                canvasId: 'sleepChartNoise',
                field: 'ambient_noise',
                yTitle: 'Ambient noise (dB)',
                color: '#5eead4',
                beginAtZero: true,
                ghostBand: [34, 52],
                phase: 1.1,
            },
            {
                wrapId: 'sleep-wrap-gyro_variance',
                canvasId: 'sleepChartGyro',
                field: 'restlessness_score',
                yTitle: 'Restful efficiency',
                scoreHundred: true,
                color: '#d4af37',
                beginAtZero: true,
                ghostBand: [62, 94],
                phase: 2.5,
            },
        ];

        for (var di = 0; di < detailSpecs.length; di++) {
            (function () {
                var spec = detailSpecs[di];
                var canvas = document.getElementById(spec.canvasId);
                if (!canvas) return;

                var metricEmpty = !sessionHasSamples || nightFiniteCount(rows, spec.field) === 0;
                var ghostOnly = metricEmpty || !sessionHasSamples;
                var capMsg = ghostOnly
                    ? (sessionHasSamples ? SLEEP_METRIC_GAP_MSG : SLEEP_ONSET_GUIDE_MSG)
                    : '';
                setSleepChartGuide(spec.wrapId, ghostOnly, capMsg);

                var dataPts = metricEmpty ? [] : mapNightPoints(rows, spec.field);
                var overlayPts = ghostOnly
                    ? ghostSinMetricPoints(
                        tMin,
                        tMax,
                        spec.ghostBand[0],
                        spec.ghostBand[1],
                        spec.phase,
                        56
                    )
                    : [];

                var yScale = {
                    title: { display: true, text: spec.yTitle, color: 'rgba(255,255,255,0.65)' },
                    beginAtZero: !!spec.beginAtZero,
                    ticks: { color: 'rgba(255,255,255,0.45)' },
                    grid: { color: 'rgba(255,255,255,0.03)', lineWidth: 1 },
                };
                if (spec.spo2Band) {
                    yScale.suggestedMin = 88;
                    yScale.suggestedMax = 100;
                }
                if (spec.scoreHundred) {
                    yScale.suggestedMin = 0;
                    yScale.suggestedMax = 100;
                }

                var datasets = [];
                if (ghostOnly) {
                        datasets.push({
                        label: spec.yTitle,
                        data: overlayPts,
                        borderColor: 'rgba(255,255,255,0.18)',
                        backgroundColor: 'transparent',
                        spanGaps: true,
                        tension: 0.15,
                        pointRadius: 0,
                        borderWidth: 1.35,
                        borderDash: [6, 7],
                        fill: false,
                    });
                } else {
                    datasets.push({
                        label: spec.yTitle,
                        data: dataPts,
                        borderColor: spec.color,
                        backgroundColor: spec.color + '18',
                        spanGaps: true,
                        tension: 0.15,
                        pointRadius: sparse ? 0 : 2,
                        borderWidth: 1.5,
                        fill: false,
                    });
                }

                var detailTooltip = {
                    mode: 'index',
                    intersect: false,
                    callbacks: {
                        title: sleepNightTooltipTitleLocal,
                    },
                };
                if (spec.field === 'restlessness_score') {
                    detailTooltip.callbacks.label = function (ctx) {
                        var r = ctx.raw;
                        if (r && Number.isFinite(r.y)) {
                            var band = (r.restlessness) ? r.restlessness : formatRestlessnessJs(r.y);
                            return ' Restful efficiency: ' + Number(r.y).toFixed(1) + ' /100 · ' + band;
                        }
                        return ' Restful efficiency: -';
                    };
                }

                var ch2 = new Chart(canvas, {
                    type: 'line',
                    data: { datasets: datasets },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        interaction: { mode: 'index', intersect: false },
                        scales: {
                            x: {
                                type: 'time',
                                min: tMin,
                                max: tMax,
                                title: {
                                    display: true,
                                    text: 'Time (local)',
                                    color: 'rgba(255,255,255,0.5)',
                                },
                                ticks: {
                                    maxRotation: 0,
                                    autoSkip: true,
                                    color: 'rgba(255,255,255,0.45)',
                                    callback: function (v) {
                                        var n = typeof v === 'number' ? v : NaN;
                                        if (!Number.isFinite(n) && typeof v === 'string') {
                                            n = Date.parse(v);
                                        }
                                        if (!Number.isFinite(n)) return '';
                                        return formatLocalClock(n);
                                    },
                                },
                                grid: { color: 'rgba(255,255,255,0.03)', lineWidth: 1 },
                                time: { tooltipFormat: 'MMM d HH:mm' },
                            },
                            y: yScale,
                        },
                        plugins: {
                            legend: { display: false },
                            tooltip: detailTooltip,
                            zoom: {
                                pan: {
                                    enabled: true,
                                    mode: 'x',
                                },
                                zoom: {
                                    wheel: { enabled: true },
                                    pinch: { enabled: true },
                                    mode: 'x',
                                },
                                limits: {
                                    x: { min: tMin, max: tMax },
                                },
                            },
                        },
                    },
                });
                sleepNightCharts.push(ch2);
            })();
        }

        attachSleepNightAxisSync(sleepNightCharts);
    }

    function destroySleepWeeklyCharts() {
        if (sleepWeeklyMultiChart) {
            try {
                sleepWeeklyMultiChart.destroy();
            } catch (e) { /* noop */ }
            sleepWeeklyMultiChart = null;
        }
        const root = document.getElementById('sleep-weekly-charts-root');
        if (root) root.innerHTML = '';
    }

    async function loadWeeklyTrendsChart() {
        const st = document.getElementById('sleep-weekly-status');
        const meansEl = document.getElementById('sleep-weekly-means-summary');
        const root = document.getElementById('sleep-weekly-charts-root');
        if (st) st.innerText = '';
        if (meansEl) meansEl.textContent = '';
        if (!root) return;

        destroySleepWeeklyCharts();

        let payload;
        try {
            const res = await fetch('/api/sleep-readiness/weekly-summary');
            payload = await res.json();
            if (!res.ok) throw new Error(payload.error || res.statusText);
        } catch (err) {
            console.error(err);
            if (st) st.innerText = 'Could not load weekly trends.';
            return;
        }

        const days = payload.days || [];
        if (!days.length) {
            if (st) {
                st.innerText =
                    'No scored sleep sessions yet. After a completed night with a sleep score, charts appear here.';
            }
            return;
        }

        const labels = days.map((d) => {
            const dl = d.display_label && String(d.display_label).trim();
            if (dl) return dl.length > 22 ? `${dl.slice(0, 20)}…` : dl;
            return d.score_date ? String(d.score_date) : '-';
        });

        function normalizeInWeek(values) {
            const nums = values.map((v) => {
                if (v === null || v === undefined || v === '') return null;
                const n = Number(v);
                return Number.isFinite(n) ? n : null;
            });
            const present = nums.filter((x) => x !== null);
            if (!present.length) return nums.map(() => null);
            const lo = Math.min(...present);
            const hi = Math.max(...present);
            const span = hi - lo;
            return nums.map((x) => {
                if (x === null) return null;
                if (span < 1e-9) return 50;
                return ((x - lo) / span) * 100;
            });
        }

        function formatWeeklyRaw(val, decimals, suffix) {
            if (val === null || val === undefined || val === '') return '-';
            const n = Number(val);
            if (!Number.isFinite(n)) return '-';
            if (decimals <= 0) return `${Math.round(n)}${suffix}`;
            return `${n.toFixed(decimals)}${suffix}`;
        }

        /** Same keys as Single Night timelines / server ``_session_sensor_nightly_averages`` (+ sleep score). */
        const weeklyLineSpecs = [
            { key: 'readiness_score', label: 'Sleep score', decimals: 1, suffix: '' },
            { key: 'avg_heart_rate_bpm', label: 'Heart rate', decimals: 1, suffix: ' bpm' },
            { key: 'avg_spo2_pct', label: 'SpO₂', decimals: 1, suffix: '%' },
            { key: 'avg_air_quality_index', label: 'Air index (VOC)', decimals: 1, suffix: '' },
            { key: 'avg_restlessness_score', label: 'Restful efficiency', decimals: 1, suffix: '' },
            { key: 'avg_hrv_rmssd_ms', label: 'HRV RMSSD', decimals: 1, suffix: ' ms' },
            { key: 'avg_prv_ms', label: 'PRV', decimals: 1, suffix: ' ms' },
            { key: 'avg_ambient_noise_db', label: 'Noise', decimals: 1, suffix: ' dB' },
            { key: 'avg_ambient_light_lux', label: 'Room light', decimals: 0, suffix: ' lx' },
        ];

        const paletteLine = [
            'rgba(212, 175, 55, 0.95)',
            'rgba(244, 114, 182, 0.92)',
            'rgba(56, 189, 248, 0.92)',
            'rgba(52, 211, 153, 0.92)',
            'rgba(167, 139, 250, 0.92)',
            'rgba(251, 146, 60, 0.92)',
            'rgba(94, 234, 212, 0.92)',
            'rgba(248, 113, 113, 0.88)',
            'rgba(190, 242, 100, 0.88)',
        ];

        const datasets = [];
        weeklyLineSpecs.forEach((spec, idx) => {
            const raw = days.map((row) => row[spec.key]);
            const hasAny = raw.some((v) => {
                if (v === null || v === undefined || v === '') return false;
                return Number.isFinite(Number(v));
            });
            if (!hasAny) return;
            const norm = normalizeInWeek(raw);
            datasets.push({
                label: spec.label,
                data: norm,
                rawValues: raw,
                spec,
                borderColor: paletteLine[idx % paletteLine.length],
                backgroundColor: 'transparent',
                tension: 0.28,
                spanGaps: false,
                pointRadius: 4,
                pointHoverRadius: 6,
                pointBackgroundColor: paletteLine[idx % paletteLine.length],
                borderWidth: 2,
                fill: false,
            });
        });

        if (!datasets.length) {
            if (st) st.innerText = 'No sensor averages available in this window yet.';
            return;
        }

        const wm = payload.weekly_means || {};
        const meanParts = [];
        weeklyLineSpecs.forEach((spec) => {
            const v = wm[spec.key];
            if (v === null || v === undefined || v === '') return;
            const n = Number(v);
            if (!Number.isFinite(n)) return;
            meanParts.push(`${spec.label} ${formatWeeklyRaw(n, spec.decimals, spec.suffix)}`);
        });
        if (meansEl && meanParts.length) {
            const nNights = payload.nights_in_window != null ? payload.nights_in_window : days.length;
            meansEl.textContent = `Simple mean across these ${nNights} night(s): ${meanParts.join(' · ')}`;
        }

        const wrap = document.createElement('div');
        wrap.className = 'relative flex flex-col rounded-xl border border-white/10 bg-black/35 p-3 shadow-inner shadow-black/30';
        const host = document.createElement('div');
        host.className = 'relative min-h-[300px] h-[360px] w-full sm:h-[400px]';
        const canvas = document.createElement('canvas');
        canvas.setAttribute('role', 'img');
        canvas.setAttribute('aria-label', 'Weekly multi-metric sleep trends');
        host.appendChild(canvas);
        wrap.appendChild(host);
        root.appendChild(wrap);

        sleepWeeklyMultiChart = new Chart(canvas, {
            type: 'line',
            data: {
                labels,
                datasets,
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: {
                        display: true,
                        position: 'bottom',
                        labels: {
                            boxWidth: 10,
                            padding: 8,
                            font: { size: 10 },
                            color: 'rgba(228,228,231,0.85)',
                        },
                    },
                    tooltip: {
                        callbacks: {
                            title(items) {
                                const i = items && items[0] && items[0].dataIndex;
                                if (i == null || !days[i]) return '';
                                const row = days[i];
                                const full = row.display_label && String(row.display_label).trim();
                                return full || (row.score_date ? String(row.score_date) : '');
                            },
                            label(ctx) {
                                const ds = ctx.dataset;
                                const spec = ds.spec;
                                const i = ctx.dataIndex;
                                const rawV = ds.rawValues ? ds.rawValues[i] : null;
                                const yRel = ctx.parsed && ctx.parsed.y;
                                const rawStr = formatWeeklyRaw(rawV, spec.decimals, spec.suffix);
                                const relStr = Number.isFinite(yRel) ? `${Math.round(yRel)}` : '-';
                                return ` ${ds.label}: ${rawStr} (within-week ${relStr}%)`;
                            },
                        },
                    },
                },
                scales: {
                    x: {
                        ticks: {
                            color: 'rgba(255,255,255,0.5)',
                            maxRotation: 35,
                            minRotation: 0,
                            autoSkip: true,
                        },
                        grid: { color: 'rgba(255,255,255,0.06)' },
                    },
                    y: {
                        min: 0,
                        max: 100,
                        ticks: { color: 'rgba(255,255,255,0.5)' },
                        title: {
                            display: true,
                            text: 'Within-week relative position (0 to 100 per metric)',
                            color: 'rgba(255,255,255,0.55)',
                            font: { size: 11 },
                        },
                        grid: { color: 'rgba(255,255,255,0.06)' },
                    },
                },
            },
        });

        if (st) st.innerText = '';
    }

    async function loadSleepScoreHistoryChart() {
        const st = document.getElementById('sleep-score-history-status');
        const canvas = document.getElementById('sleepScoreHistoryChart');
        if (!canvas) return;

        if (sleepScoreHistoryChart) {
            try {
                sleepScoreHistoryChart.destroy();
            } catch (e) { /* noop */ }
            sleepScoreHistoryChart = null;
        }

        if (st) st.innerText = 'Loading…';

        let payload;
        try {
            const res = await fetch('/api/sleep-readiness/history?limit=60');
            payload = await res.json();
            if (!res.ok) throw new Error(payload.error || res.statusText);
        } catch (err) {
            console.error(err);
            if (st) st.innerText = `Could not load sleep scores. ${err.message}`;
            return;
        }

        const rowsDesc = payload.scores || [];
        if (!rowsDesc.length) {
            if (st) {
                st.innerText =
                    'No sleep scores yet. After at least one completed sleep session, your trend will appear here.';
            }
            return;
        }

        // API returns newest-first; keep one point per sleep date (most recent session wins).
        const byDay = new Map();
        rowsDesc.forEach((s) => {
            const d = s && s.score_date;
            if (!d || s.readiness_score === null || s.readiness_score === undefined) return;
            byDay.set(d, s);
        });
        const series = Array.from(byDay.values()).sort((a, b) =>
            String(a.score_date).localeCompare(String(b.score_date)),
        );

        const points = series.map((s) => {
            const dt = parseUtcTimestamp(`${s.score_date}T12:00:00Z`);
            const xv = dt && !Number.isNaN(dt.getTime()) ? dt.getTime() : null;
            const y = Number(s.readiness_score);
            return xv !== null && Number.isFinite(y) ? { x: xv, y } : null;
        }).filter(Boolean);

        if (!points.length) {
            if (st) st.innerText = 'No sleep scores could be plotted from your history yet.';
            return;
        }

        sleepScoreHistoryChart = new Chart(canvas, {
            type: 'line',
            data: {
                datasets: [{
                    label: 'Sleep score',
                    data: points,
                    borderColor: 'rgba(212, 175, 55, 0.92)',
                    backgroundColor: 'rgba(212, 175, 55, 0.14)',
                    borderWidth: 2,
                    fill: true,
                    tension: 0.28,
                    pointRadius: 3.5,
                    pointHoverRadius: 6,
                    pointBackgroundColor: 'rgba(212, 175, 55, 0.95)',
                    pointBorderColor: 'rgba(11, 14, 17, 0.85)',
                    pointBorderWidth: 1,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    x: {
                        type: 'time',
                        time: {
                            unit: 'day',
                            tooltipFormat: 'MMM d, yyyy',
                            displayFormats: { day: 'MMM d' },
                        },
                        ticks: {
                            maxRotation: 0,
                            autoSkip: true,
                            color: 'rgba(255,255,255,0.45)',
                        },
                        grid: { color: 'rgba(255,255,255,0.04)', lineWidth: 1 },
                    },
                    y: {
                        type: 'linear',
                        min: 0,
                        max: 100,
                        title: {
                            display: true,
                            text: 'Sleep score',
                            color: 'rgba(255,255,255,0.65)',
                        },
                        ticks: { color: 'rgba(255,255,255,0.5)' },
                        grid: { color: 'rgba(255,255,255,0.04)', lineWidth: 1 },
                    },
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        mode: 'index',
                        intersect: false,
                        callbacks: {
                            title(items) {
                                const it = items && items[0];
                                if (!it || !it.parsed || it.parsed.x === undefined) return '';
                                const d = new Date(it.parsed.x);
                                return Number.isNaN(d.getTime())
                                    ? ''
                                    : d.toLocaleDateString(undefined, {
                                        weekday: 'short',
                                        year: 'numeric',
                                        month: 'short',
                                        day: 'numeric',
                                    });
                            },
                            label(ctx) {
                                const v = ctx.parsed && ctx.parsed.y;
                                if (!Number.isFinite(v)) return ' Sleep score: -';
                                return ` Sleep score: ${Math.round(v)}`;
                            },
                        },
                    },
                },
            },
        });

        if (st) st.innerText = '';
    }

    function updateSleepHistoryModeUI() {
        const mode = document.getElementById('sleep-history-mode').value;
        const nightLbl = document.getElementById('sleep-session-select-label');
        const single = document.getElementById('sleep-single-night-wrap');
        const weekly = document.getElementById('sleep-weekly-wrap');
        const nightMode = mode === 'single-night';
        if (nightLbl) {
            nightLbl.classList.toggle('hidden', !nightMode);
        }
        single.classList.toggle('hidden', !nightMode);
        weekly.classList.toggle('hidden', nightMode);
        if (nightMode) {
            destroySleepWeeklyCharts();
            loadSingleNightCharts();
        } else {
            destroySleepNightCharts();
            loadWeeklyTrendsChart();
        }
    }

    document.getElementById('sleep-history-mode').addEventListener('change', updateSleepHistoryModeUI);
    document.getElementById('sleep-session-select').addEventListener('change', () => {
        if (document.getElementById('sleep-history-mode').value === 'single-night') {
            loadSingleNightCharts();
        }
    });
    document.getElementById('sleep-night-reset-zoom').addEventListener('click', () => {
        sleepNightCharts.forEach((c) => {
            try {
                if (typeof c.resetZoom === 'function') {
                    c.resetZoom();
                }
            } catch (e) {
                console.warn('resetZoom skipped', e);
            }
        });
    });

    updateSleepHistoryModeUI();

    setInterval(() => {
        fetch('/api/latest-readings')
            .then((response) => {
                if (!response.ok) throw new Error(response.statusText);
                return response.json();
            })
            .then(applyLiveReadingsPayload)
            .catch((error) => console.error('Error fetching data:', error));
    }, 5000);

    setInterval(() => {
        if (liveReadingsCache) applyLiveReadingsPayload(liveReadingsCache);
    }, 20000);

    // Convert event-log table instants from stored form to the viewer's local time
    document.querySelectorAll('.utc-table-time').forEach(el => {
        const exactTime = parseUtcTimestamp(el.innerText);
        if (exactTime && !Number.isNaN(exactTime.getTime())) {
            el.innerText = exactTime.toLocaleString();
        }
    });

    // Configuration Panel - comfort range slider (°F guardrails + optimal band)
    const tempComfortDefaults = Object.freeze({
        guardMin: 60,
        guardMax: 75,
        optMin: 65,
        optMax: 68,
    });

    let tempComfort = {
        guardMin: tempComfortDefaults.guardMin,
        guardMax: tempComfortDefaults.guardMax,
        optMin: tempComfortDefaults.optMin,
        optMax: tempComfortDefaults.optMax,
    };

    let comfortBandDragging = false;
    let comfortDragOrigin = null;

    function syncComfortInputsToState() {
        const gMi = parseFloat(document.getElementById('cfg-guardrail-f-min').value);
        const gMa = parseFloat(document.getElementById('cfg-guardrail-f-max').value);
        if (Number.isFinite(gMi)) tempComfort.guardMin = gMi;
        if (Number.isFinite(gMa)) tempComfort.guardMax = gMa;
    }

    function clampComfortGuardrails() {
        if (tempComfort.guardMin >= tempComfort.guardMax) {
            const t = tempComfort.guardMin;
            tempComfort.guardMin = Math.min(t, tempComfort.guardMax - 1);
            tempComfort.guardMax = Math.max(t + 1, tempComfort.guardMax);
        }
        tempComfort.guardMin = Math.max(
            40,
            Math.min(109, tempComfort.guardMin)
        );
        tempComfort.guardMax = Math.max(
            tempComfort.guardMin + 0.5,
            Math.min(110, tempComfort.guardMax)
        );
    }

    function clampComfortOptimal() {
        let w = tempComfort.optMax - tempComfort.optMin;
        if (w < 0.5) {
            tempComfort.optMax = tempComfort.optMin + 0.5;
            w = 0.5;
        }
        const span = tempComfort.guardMax - tempComfort.guardMin;
        w = Math.min(w, span);
        tempComfort.optMin = Math.max(
            tempComfort.guardMin,
            Math.min(tempComfort.optMin, tempComfort.guardMax - w)
        );
        tempComfort.optMax = tempComfort.optMin + w;
        if (tempComfort.optMax > tempComfort.guardMax) {
            tempComfort.optMax = tempComfort.guardMax;
            tempComfort.optMin = tempComfort.optMax - w;
            if (tempComfort.optMin < tempComfort.guardMin) {
                tempComfort.optMin = tempComfort.guardMin;
            }
        }
    }

    function parseLiveNumberForComfort(val) {
        if (val === null || val === undefined) return null;
        if (typeof val === 'number' && Number.isFinite(val)) return val;
        const s = String(val).trim();
        if (!s || s === '-' || s.toUpperCase() === 'N/A' || s === '--') return null;
        const n = Number(s);
        return Number.isFinite(n) ? n : null;
    }

    function celsiusToFahrenheitForComfort(c) {
        return c * (9 / 5) + 32;
    }

    function updateTemperatureComfortFromCache() {
        const el = document.querySelector('.temp-val');
        if (!el) return;
        const data = liveReadingsCache;
        if (!data || liveReadingsLooksEmpty(data)) {
            el.className = DASH_TEMP_TIER_CLASS.neutral;
            return;
        }
        const c = parseLiveNumberForComfort(data.temperature);
        if (c === null) {
            el.className = DASH_TEMP_TIER_CLASS.neutral;
            return;
        }
        syncComfortInputsToState();
        clampComfortGuardrails();
        clampComfortOptimal();
        const f = celsiusToFahrenheitForComfort(c);
        let tier = 'amber';
        if (f >= tempComfort.optMin && f <= tempComfort.optMax) tier = 'ok';
        else if (f < tempComfort.guardMin || f > tempComfort.guardMax) tier = 'red';
        const tierCls = {
            ok: DASH_TEMP_TIER_CLASS.ok,
            amber: DASH_TEMP_TIER_CLASS.amber,
            red: DASH_TEMP_TIER_CLASS.red,
        };
        el.className = tierCls[tier] || DASH_TEMP_TIER_CLASS.neutral;
    }

    function writeComfortInputsFromState() {
        document.getElementById('cfg-guardrail-f-min').value = String(tempComfort.guardMin);
        document.getElementById('cfg-guardrail-f-max').value = String(tempComfort.guardMax);
    }

    function renderComfortSlider() {
        const track = document.getElementById('cfg-temp-range-track');
        const band = document.getElementById('cfg-temp-range-optimal');
        const overrideOn = document.getElementById('cfg-override-optimal').checked;
        const gmin = tempComfort.guardMin;
        const gmax = tempComfort.guardMax;
        const span = gmax - gmin;
        if (span <= 0) {
            updateTemperatureComfortFromCache();
            return;
        }

        const leftPct = ((tempComfort.optMin - gmin) / span) * 100;
        const widthPct = ((tempComfort.optMax - tempComfort.optMin) / span) * 100;

        band.style.left = `${Math.max(0, Math.min(leftPct, 100))}%`;
        band.style.width = `${Math.max(0, Math.min(widthPct, 100 - leftPct))}%`;

        document.getElementById('cfg-temp-range-label-low').innerText =
            `${tempComfort.guardMin.toFixed(0)}°F`;
        document.getElementById('cfg-temp-range-label-high').innerText =
            `${tempComfort.guardMax.toFixed(0)}°F`;
        document.getElementById('cfg-optimal-label').innerText =
            `Optimal ${tempComfort.optMin.toFixed(1)}-${tempComfort.optMax.toFixed(1)} °F`;

        band.classList.toggle('cfg-temp-range-draggable', overrideOn && !comfortBandDragging);
        if (overrideOn) {
            band.setAttribute('title', 'Drag to slide optimal band inside guardrails');
        } else {
            band.removeAttribute('title');
        }
        updateTemperatureComfortFromCache();
    }

    function attachComfortSliderHandlers() {
        const gminEl = document.getElementById('cfg-guardrail-f-min');
        const gmaxEl = document.getElementById('cfg-guardrail-f-max');
        const band = document.getElementById('cfg-temp-range-optimal');
        const track = document.getElementById('cfg-temp-range-track');
        const overrideEl = document.getElementById('cfg-override-optimal');

        const onGuardrailChange = () => {
            syncComfortInputsToState();
            clampComfortGuardrails();
            clampComfortOptimal();
            writeComfortInputsFromState();
            renderComfortSlider();
        };

        gminEl.addEventListener('change', onGuardrailChange);
        gmaxEl.addEventListener('change', onGuardrailChange);
        gminEl.addEventListener('input', onGuardrailChange);
        gmaxEl.addEventListener('input', onGuardrailChange);

        overrideEl.addEventListener('change', () => {
            syncComfortInputsToState();
            clampComfortGuardrails();
            clampComfortOptimal();
            writeComfortInputsFromState();
            renderComfortSlider();
        });

        function beginComfortBandDrag(clientX) {
            comfortBandDragging = true;
            band.classList.add('cfg-temp-range-dragging');
            const rect = track.getBoundingClientRect();
            const span = tempComfort.guardMax - tempComfort.guardMin;
            comfortDragOrigin = {
                clientX0: clientX,
                optMin0: tempComfort.optMin,
                bandWidth: Math.max(tempComfort.optMax - tempComfort.optMin, 0.5),
                trackWidth: Math.max(rect.width, 1),
                guardSpan: Math.max(span, 0.5),
                guardMin: tempComfort.guardMin,
                guardMax: tempComfort.guardMax,
            };
        }

        function comfortBandApplyClientX(clientX) {
            if (!comfortBandDragging || !comfortDragOrigin) return;
            const d = comfortDragOrigin;
            const dx = clientX - d.clientX0;
            const deltaF = (dx / d.trackWidth) * (d.guardMax - d.guardMin);
            let nMin = d.optMin0 + deltaF;
            const gw = Math.max(d.bandWidth, 0.5);
            nMin = Math.max(d.guardMin, Math.min(nMin, d.guardMax - gw));
            tempComfort.optMin = nMin;
            tempComfort.optMax = nMin + gw;
            renderComfortSlider();
        }

        band.addEventListener('mousedown', (event) => {
            if (!overrideEl.checked) return;
            event.preventDefault();
            beginComfortBandDrag(event.clientX);
        });

        band.addEventListener(
            'touchstart',
            (event) => {
                if (!overrideEl.checked) return;
                if (!event.touches || event.touches.length === 0) return;
                event.preventDefault();
                beginComfortBandDrag(event.touches[0].clientX);
            },
            { passive: false },
        );

        window.addEventListener('mousemove', (event) => {
            comfortBandApplyClientX(event.clientX);
        });

        window.addEventListener(
            'touchmove',
            (event) => {
                if (!comfortBandDragging || !comfortDragOrigin) return;
                const t = event.touches && event.touches[0];
                if (!t) return;
                event.preventDefault();
                comfortBandApplyClientX(t.clientX);
            },
            { passive: false },
        );

        function finishComfortBandDragIfNeeded() {
            if (!comfortBandDragging) return;
            comfortBandDragging = false;
            comfortDragOrigin = null;
            band.classList.remove('cfg-temp-range-dragging');
            renderComfortSlider();
        }

        window.addEventListener('mouseup', finishComfortBandDragIfNeeded);
        window.addEventListener('touchend', finishComfortBandDragIfNeeded, { passive: true });
        window.addEventListener('touchcancel', finishComfortBandDragIfNeeded, { passive: true });
    }

    function refreshCfgWakeTimeCurrentLabel() {
        const el = document.getElementById('cfg-wake-time-current');
        if (!el) return;
        const v = String(savedCfgWakeTime || '').trim();
        el.textContent = v || 'Not set';
    }

    // Configuration Panel (persisted per authenticated user)
    async function initializeConfigPanel() {
        try {
            const response = await fetch('/api/user-config');
            const cfg = await response.json();
            if (!response.ok) throw new Error(cfg.error || 'Failed to load configuration');

            document.getElementById('cfg-wake-time').value = cfg.wake_time ?? '';
            savedCfgWakeTime = String(cfg.wake_time ?? '').trim();
            refreshCfgWakeTimeCurrentLabel();

            tempComfort.guardMin =
                cfg.guardrail_temp_f_min ?? tempComfortDefaults.guardMin;
            tempComfort.guardMax =
                cfg.guardrail_temp_f_max ?? tempComfortDefaults.guardMax;
            tempComfort.optMin =
                cfg.optimal_band_f_min ?? tempComfortDefaults.optMin;
            tempComfort.optMax =
                cfg.optimal_band_f_max ?? tempComfortDefaults.optMax;
            document.getElementById('cfg-override-optimal').checked =
                !!cfg.override_optimal_band;

            writeComfortInputsFromState();
            clampComfortGuardrails();
            clampComfortOptimal();
            writeComfortInputsFromState();
            renderComfortSlider();

            document.getElementById('config-status').innerText = 'Loaded account configuration.';
            updateMinutesToWakeDisplay();
        } catch (err) {
            console.error('Failed to load configuration panel settings:', err);
            document.getElementById('config-status').innerText = 'Unable to load saved config.';
        }
    }

    async function saveConfigPanel() {
        syncComfortInputsToState();
        clampComfortGuardrails();
        clampComfortOptimal();
        writeComfortInputsFromState();
        renderComfortSlider();

        const configPayload = {
            wake_time: document.getElementById('cfg-wake-time').value,
            guardrail_temp_f_min: tempComfort.guardMin,
            guardrail_temp_f_max: tempComfort.guardMax,
            optimal_band_f_min: tempComfort.optMin,
            optimal_band_f_max: tempComfort.optMax,
            override_optimal_band:
                document.getElementById('cfg-override-optimal').checked,
        };

        try {
            const response = await fetch('/api/user-config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify(configPayload),
            });
            const result = await response.json();
            if (!response.ok) throw new Error(result.error || 'Save failed');
            document.getElementById('config-status').innerText =
                `Saved to account at ${new Date().toLocaleTimeString()}`;
            savedCfgWakeTime = String(
                document.getElementById('cfg-wake-time').value || '',
            ).trim();
            refreshCfgWakeTimeCurrentLabel();
            updateMinutesToWakeDisplay();
        } catch (err) {
            console.error('Failed to save configuration:', err);
            document.getElementById('config-status').innerText =
                `Save failed: ${err.message}`;
        }
    }

    async function resetConfigPanel() {
        document.getElementById('cfg-wake-time').value = '';
        document.getElementById('cfg-override-optimal').checked = false;
        tempComfort = {
            guardMin: tempComfortDefaults.guardMin,
            guardMax: tempComfortDefaults.guardMax,
            optMin: tempComfortDefaults.optMin,
            optMax: tempComfortDefaults.optMax,
        };
        writeComfortInputsFromState();
        renderComfortSlider();
        await saveConfigPanel();
    }

    let morningReviewState = { target_session_date: null, review: null };

    function formatMorningReviewStars(n) {
        const r = Number(n);
        if (!Number.isFinite(r)) return '';
        const full = Math.max(1, Math.min(5, Math.round(r)));
        let s = '';
        for (let i = 1; i <= 5; i += 1) {
            s += i <= full ? '★' : '☆';
        }
        return s;
    }

    function renderMorningReviewSummary(review) {
        const wrap = document.getElementById('morning-review-star-visual');
        wrap.textContent = '';
        const starSpan = document.createElement('span');
        starSpan.className = 'tracking-[0.15em] text-amber-400';
        starSpan.title = `${review.rating} of 5 stars`;
        starSpan.textContent = formatMorningReviewStars(review.rating);
        wrap.appendChild(starSpan);
        document.getElementById('morning-review-rating-num').textContent = `${review.rating} / 5`;
        const algoWrap = document.getElementById('morning-review-algorithm-wrap');
        const algoScore = document.getElementById('morning-review-algorithm-score');
        const ar = review.algorithm_readiness_at_submit;
        if (algoWrap && algoScore) {
            if (ar !== null && ar !== undefined && ar !== '') {
                algoWrap.classList.remove('hidden');
                algoScore.textContent = String(ar);
            } else {
                algoWrap.classList.add('hidden');
                algoScore.textContent = '-';
            }
        }
        const noteEl = document.getElementById('morning-review-note-display');
        noteEl.classList.remove('has-notes', 'no-notes');
        if (review.notes && review.notes.trim()) {
            noteEl.textContent = review.notes;
            noteEl.classList.add('has-notes');
        } else {
            noteEl.textContent = '(No notes)';
            noteEl.classList.add('no-notes');
        }
        morningReviewState.review = review;
    }

    async function refreshMorningReviewCard() {
        const loadEl = document.getElementById('morning-review-loading');
        const emptyEl = document.getElementById('morning-review-empty');
        const sumEl = document.getElementById('morning-review-summary');
        const editEl = document.getElementById('morning-review-edit');
        const statusEl = document.getElementById('morning-review-status-msg');
        statusEl.innerText = '';
        loadEl.classList.remove('hidden');
        emptyEl.classList.add('hidden');
        sumEl.classList.add('hidden');
        editEl.classList.add('hidden');
        try {
            const res = await fetch('/api/subjective-sleep-review/status', { credentials: 'same-origin' });
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || res.statusText);
            morningReviewState.target_session_date = data.target_session_date;
            const label = document.getElementById('morning-review-session-label');
            const d = parseUtcTimestamp(`${data.target_session_date}T12:00:00Z`);
            label.textContent = d && !Number.isNaN(d.getTime())
                ? d.toLocaleDateString(
                    undefined,
                    { weekday: 'short', year: 'numeric', month: 'short', day: 'numeric' },
                )
                : data.target_session_date;

            loadEl.classList.add('hidden');
            if (!data.has_review) {
                emptyEl.classList.remove('hidden');
                document.getElementById('morning-review-form-new').reset();
            } else {
                renderMorningReviewSummary(data.review);
                sumEl.classList.remove('hidden');
            }
        } catch (e) {
            console.error(e);
            loadEl.classList.add('hidden');
            emptyEl.classList.remove('hidden');
            statusEl.innerText = `Could not load review status: ${e.message}`;
        }
    }

    async function submitMorningReviewNew() {
        const statusEl = document.getElementById('morning-review-status-msg');
        const rating = document.getElementById('morning-review-rating-new').value;
        if (!rating) {
            statusEl.innerText = 'Please choose a star rating.';
            return;
        }
        statusEl.innerText = 'Saving…';
        try {
            const res = await fetch('/api/subjective-sleep-review', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    rating: parseInt(rating, 10),
                    notes: document.getElementById('morning-review-notes-new').value.trim() || undefined,
                }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.error || res.statusText);
            document.getElementById('morning-review-empty').classList.add('hidden');
            renderMorningReviewSummary(data.review);
            document.getElementById('morning-review-summary').classList.remove('hidden');
            document.getElementById('morning-review-form-new').reset();
            let msg = `Saved review for wake session ${data.review.session_date}.`;
            if (data.discrepancy_logged) {
                msg += ' A low-rating vs high sleep-score mismatch was logged for future formula tuning.';
            }
            statusEl.innerText = msg;
            if (isMorningReviewHistoryTabActive()) loadMorningReviewHistory();
        } catch (e) {
            statusEl.innerText = `Save failed: ${e.message}`;
        }
    }

    async function submitMorningReviewEdit() {
        const statusEl = document.getElementById('morning-review-status-msg');
        const sessionDate = document.getElementById('morning-review-session-date-edit').value;
        const rating = document.getElementById('morning-review-rating-edit').value;
        statusEl.innerText = 'Saving…';
        try {
            const res = await fetch('/api/subjective-sleep-review', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    rating: parseInt(rating, 10),
                    notes: document.getElementById('morning-review-notes-edit').value.trim() || undefined,
                    session_date: sessionDate,
                }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.error || res.statusText);
            renderMorningReviewSummary(data.review);
            document.getElementById('morning-review-edit').classList.add('hidden');
            document.getElementById('morning-review-summary').classList.remove('hidden');
            let msg = `Updated review for wake session ${data.review.session_date}.`;
            if (data.discrepancy_logged) {
                msg += ' A low-rating vs high sleep-score mismatch was logged for future formula tuning.';
            }
            statusEl.innerText = msg;
            if (isMorningReviewHistoryTabActive()) loadMorningReviewHistory();
        } catch (e) {
            statusEl.innerText = `Save failed: ${e.message}`;
        }
    }

    function morningReviewMoonSvg() {
        return '<svg class="mr-rail-icon mr-rail-icon--moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
    }

    function morningReviewBatterySvg() {
        return '<svg class="mr-rail-icon mr-rail-icon--battery" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="7" y="6" width="10" height="15" rx="2"/><path d="M10 4h4v2h-4z"/></svg>';
    }

    function isMorningReviewHistoryTabActive() {
        const hist = document.getElementById('morning-review-tab-history-panel');
        return hist && !hist.hasAttribute('hidden');
    }

    function setMorningReviewTab(tab) {
        const todayPanel = document.getElementById('morning-review-tab-today-panel');
        const histPanel = document.getElementById('morning-review-tab-history-panel');
        const bToday = document.getElementById('morning-review-tab-today-btn');
        const bHist = document.getElementById('morning-review-tab-history-btn');
        if (!todayPanel || !histPanel || !bToday || !bHist) return;
        const showHistory = tab === 'history';
        if (showHistory) {
            todayPanel.setAttribute('hidden', '');
            histPanel.removeAttribute('hidden');
        } else {
            todayPanel.removeAttribute('hidden');
            histPanel.setAttribute('hidden', '');
        }
        const btnBase =
            'flex-1 rounded-xl py-2.5 text-center text-[10px] font-bold uppercase tracking-widest transition-all duration-200';
        const active =
            `${btnBase} text-zinc-100 shadow-inner ring-1 ring-amber-500/25 bg-amber-500/15`;
        const inactive =
            `${btnBase} text-zinc-500 hover:text-zinc-300`;
        bToday.className = showHistory ? inactive : active;
        bHist.className = showHistory ? active : inactive;
        bToday.setAttribute('aria-selected', showHistory ? 'false' : 'true');
        bHist.setAttribute('aria-selected', showHistory ? 'true' : 'false');
        if (showHistory) loadMorningReviewHistory();
    }

    async function loadMorningReviewHistory() {
        const wrap = document.getElementById('morning-review-history-list-wrap');
        const loadEl = document.getElementById('morning-review-history-loading');
        const emptyEl = document.getElementById('morning-review-history-empty');
        if (!wrap) return;
        wrap.innerHTML = '';
        if (emptyEl) emptyEl.classList.add('hidden');
        if (loadEl) loadEl.classList.remove('hidden');
        try {
            const res = await fetch('/api/subjective-sleep-review/history?days=7&limit=14', {
                credentials: 'same-origin',
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || res.statusText);
            const rows = data.reviews || [];
            rows.forEach((rev) => {
                const card = document.createElement('article');
                card.className = 'morning-review-history-card';

                const dt = parseUtcTimestamp(`${rev.session_date}T12:00:00Z`);
                const dateStr = dt && !Number.isNaN(dt.getTime())
                    ? dt.toLocaleDateString(undefined, {
                        weekday: 'short',
                        month: 'short',
                        day: 'numeric',
                        year: 'numeric',
                    })
                    : rev.session_date;

                const headline = document.createElement('div');
                headline.className = 'morning-review-history-head';
                const moonWrap = document.createElement('span');
                moonWrap.className = 'morning-review-inline-icon';
                moonWrap.setAttribute('title', 'Sleep (subjective)');
                moonWrap.innerHTML = morningReviewMoonSvg();
                const headText = document.createElement('div');
                headText.className = 'morning-review-history-head-text';
                const stars = document.createElement('span');
                stars.className = 'morning-review-history-stars';
                stars.textContent = formatMorningReviewStars(rev.rating);
                stars.title = `${rev.rating} of 5 stars`;
                const dateEl = document.createElement('time');
                dateEl.className = 'morning-review-history-date';
                dateEl.dateTime = rev.session_date;
                dateEl.textContent = dateStr;
                headText.appendChild(stars);
                headText.appendChild(dateEl);
                headline.appendChild(moonWrap);
                headline.appendChild(headText);

                const readiness = document.createElement('div');
                readiness.className = 'morning-review-history-readiness';
                const batWrap = document.createElement('span');
                batWrap.className = 'morning-review-inline-icon';
                batWrap.setAttribute('title', 'Sleep score');
                batWrap.innerHTML = morningReviewBatterySvg();
                const readyText = document.createElement('span');
                readyText.className = 'morning-review-readiness-text';
                if (rev.readiness_score !== null && rev.readiness_score !== undefined) {
                    readyText.textContent = `Sleep score ${rev.readiness_score}`;
                } else {
                    readyText.textContent = 'Sleep score -';
                    readiness.classList.add('is-missing');
                }
                readiness.appendChild(batWrap);
                readiness.appendChild(readyText);

                const notes = document.createElement('p');
                notes.className = 'morning-review-history-notes';
                if (rev.notes && rev.notes.trim()) {
                    notes.textContent = rev.notes;
                } else {
                    notes.classList.add('is-empty');
                    notes.textContent = 'No notes';
                }

                card.appendChild(headline);
                card.appendChild(readiness);
                card.appendChild(notes);
                wrap.appendChild(card);
            });
            if (!rows.length && emptyEl) {
                emptyEl.textContent = 'No subjective reviews in the last 7 sleep dates.';
                emptyEl.classList.remove('hidden');
            }
        } catch (e) {
            console.error(e);
            wrap.innerHTML = '';
            if (emptyEl) {
                emptyEl.textContent = `Could not load history: ${e.message}`;
                emptyEl.classList.remove('hidden');
            }
        } finally {
            if (loadEl) loadEl.classList.add('hidden');
        }
    }

    function initializeMorningReviewCard() {
        document.getElementById('morning-review-tab-today-btn').addEventListener('click', () => {
            setMorningReviewTab('today');
        });
        document.getElementById('morning-review-tab-history-btn').addEventListener('click', () => {
            setMorningReviewTab('history');
        });

        document.getElementById('morning-review-form-new').addEventListener('submit', (ev) => {
            ev.preventDefault();
            submitMorningReviewNew();
        });

        document.getElementById('morning-review-form-edit').addEventListener('submit', (ev) => {
            ev.preventDefault();
            submitMorningReviewEdit();
        });

        document.getElementById('morning-review-modify-btn').addEventListener('click', () => {
            document.getElementById('morning-review-summary').classList.add('hidden');
            document.getElementById('morning-review-edit').classList.remove('hidden');
            const r = morningReviewState.review;
            if (!r) return;
            document.getElementById('morning-review-session-date-edit').value = r.session_date;
            document.getElementById('morning-review-rating-edit').value = String(r.rating);
            document.getElementById('morning-review-notes-edit').value = r.notes || '';
            document.getElementById('morning-review-status-msg').innerText = '';
        });

        document.getElementById('morning-review-cancel-btn').addEventListener('click', () => {
            document.getElementById('morning-review-edit').classList.add('hidden');
            document.getElementById('morning-review-summary').classList.remove('hidden');
            document.getElementById('morning-review-status-msg').innerText = '';
        });

        setMorningReviewTab('today');
        refreshMorningReviewCard();
    }

    const SUNRISE_CARD_START = { r: 255, g: 140, b: 0 };
    const SUNRISE_CARD_END = { r: 240, g: 248, b: 255 };
    const SUNRISE_DEMO_SECONDS = 30;
    const LS_SUNRISE = {
        lumenMin: 'sunrise_lumen_min',
        lumenMax: 'sunrise_lumen_max',
        kelvinMin: 'sunrise_kelvin_min',
        kelvinMax: 'sunrise_kelvin_max',
    };

    /** Kelvin position in range → full-intensity tint (warm … cool). Ignored when lumens are 0. */
    function sunriseTintRgbFromKelvin(kelvin, kLo, kHi) {
        const spanK = (kHi - kLo) || 1;
        const w = Math.min(Math.max((kelvin - kLo) / spanK, 0), 1);
        const r = Math.round(SUNRISE_CARD_START.r + (SUNRISE_CARD_END.r - SUNRISE_CARD_START.r) * w);
        const g = Math.round(SUNRISE_CARD_START.g + (SUNRISE_CARD_END.g - SUNRISE_CARD_START.g) * w);
        const b = Math.round(SUNRISE_CARD_START.b + (SUNRISE_CARD_END.b - SUNRISE_CARD_START.b) * w);
        return { r, g, b };
    }

    /** 0…1 scalar from current lumen vs demo range; 0 lm ⇒ 0 (black). */
    function sunriseLumenBrightness01(lumen, lumenLo, lumenHi) {
        if (lumen <= 0) return 0;
        if (lumenHi <= lumenLo) return 1;
        const t = (lumen - lumenLo) / (lumenHi - lumenLo);
        return Math.min(Math.max(t, 0), 1);
    }

    /** Card + accents: brightness tracks lumen output; 0 lm ⇒ #000 regardless of Kelvin. */
    function sunriseRgbFromLumenKelvin(lumen, kelvin) {
        const { lumenLo, lumenHi, kLo, kHi } = getSunriseRanges();
        if (lumen <= 0) {
            return { r: 0, g: 0, b: 0 };
        }
        const bright = sunriseLumenBrightness01(lumen, lumenLo, lumenHi);
        if (bright <= 0) {
            return { r: 0, g: 0, b: 0 };
        }
        const tint = sunriseTintRgbFromKelvin(kelvin, kLo, kHi);
        return {
            r: Math.round(tint.r * bright),
            g: Math.round(tint.g * bright),
            b: Math.round(tint.b * bright),
        };
    }

    function parseSunriseInput(id, fallback) {
        const el = document.getElementById(id);
        const n = parseInt(el && el.value, 10);
        return Number.isFinite(n) ? n : fallback;
    }

    /** Sorted min/max for mapping progress to displayed lumens and Kelvin */
    function getSunriseRanges() {
        let lumenLo = parseSunriseInput('sunrise-lumen-min', 0);
        let lumenHi = parseSunriseInput('sunrise-lumen-max', 500);
        let kLo = parseSunriseInput('sunrise-kelvin-min', 2000);
        let kHi = parseSunriseInput('sunrise-kelvin-max', 6500);
        if (lumenLo > lumenHi) [lumenLo, lumenHi] = [lumenHi, lumenLo];
        if (kLo > kHi) [kLo, kHi] = [kHi, kLo];
        return { lumenLo, lumenHi, kLo, kHi };
    }

    function mapProgressToLumenKelvin(progressPercent) {
        const t01 = Math.min(Math.max(Number(progressPercent) || 0, 0), 100) / 100;
        const { lumenLo, lumenHi, kLo, kHi } = getSunriseRanges();
        const lumen = Math.round(lumenLo + (lumenHi - lumenLo) * t01);
        const kelvin = Math.round(kLo + (kHi - kLo) * t01);
        return { t01, lumen, kelvin };
    }

    function sunriseBrightnessPct(lumen, lumenLo, lumenHi) {
        const lm = Number(lumen);
        if (!Number.isFinite(lm)) return null;
        if (lm <= 0) return 0;
        if (!(Number.isFinite(lumenHi) && Number.isFinite(lumenLo) && lumenHi > lumenLo)) return null;
        return Math.min(
            100,
            Math.max(0, Math.round((100 * (lm - lumenLo)) / (lumenHi - lumenLo))),
        );
    }

    const DEV_SCENARIO_PROFILE_FLAGS = {
        cold_night: {
            force_high_temperature: false,
            force_high_noise: false,
            force_low_temperature: true,
            force_voc_spike: false,
        },
        hot_room: {
            force_high_temperature: true,
            force_high_noise: false,
            force_low_temperature: false,
            force_voc_spike: false,
        },
        noisy_room: {
            force_high_temperature: false,
            force_high_noise: true,
            force_low_temperature: false,
            force_voc_spike: false,
        },
        voc_spike: {
            force_high_temperature: false,
            force_high_noise: false,
            force_low_temperature: false,
            force_voc_spike: true,
        },
        clear: {
            force_high_temperature: false,
            force_high_noise: false,
            force_low_temperature: false,
            force_voc_spike: false,
        },
    };

    /**
     * Virtual smart-home webhook shape (physical bridge not wired in this codebase).
     * ``commands`` mirrors hardware-style actuator messages the integration layer would emit.
     */
    function renderSimulationSmartHomeIntent(devSim, options) {
        const code = document.getElementById('outbound-payload-simulation');
        if (!code) return;
        const opts = options || {};
        const pendingScenario = typeof opts.pendingScenario === 'string' ? opts.pendingScenario : null;

        let ft;
        let fn;
        let fl;
        let fv;
        if (pendingScenario && DEV_SCENARIO_PROFILE_FLAGS[pendingScenario]) {
            const p = DEV_SCENARIO_PROFILE_FLAGS[pendingScenario];
            ft = p.force_high_temperature === true;
            fn = p.force_high_noise === true;
            fl = p.force_low_temperature === true;
            fv = p.force_voc_spike === true;
        } else {
            const d = devSim || {};
            ft = d.force_high_temperature === true;
            fn = d.force_high_noise === true;
            fl = d.force_low_temperature === true;
            fv = d.force_voc_spike === true;
        }

        const commands = [];
        let wantFanMax = false;
        if (ft) {
            commands.push({ actuator: 'cooling', state: 'on' });
            wantFanMax = true;
        }
        if (fn) {
            commands.push({ actuator: 'white_noise', state: 'on' });
            wantFanMax = true;
        }
        if (fl) commands.push({ actuator: 'heater', state: 'on' });
        if (fv) {
            commands.push({ actuator: 'air_filtration', state: 'on' });
            wantFanMax = true;
        }
        if (wantFanMax) commands.push({ actuator: 'fan', speed: 'max' });

        const scenarioParts = [];
        if (ft) scenarioParts.push('forced_high_temperature');
        if (fn) scenarioParts.push('forced_high_noise');
        if (fl) scenarioParts.push('forced_low_temperature');
        if (fv) scenarioParts.push('forced_voc_spike');
        const fanoutScenario = scenarioParts.length === 0 ? 'nominal_demo' : scenarioParts.join('+');

        const dashboardsBody = pendingScenario
            ? { scenario: pendingScenario }
            : {
                force_high_temperature: ft,
                force_high_noise: fn,
                force_low_temperature: fl,
                force_voc_spike: fv,
            };

        const intent = {
            bridge: 'virtual-smart-home (intent only)',
            issued_at_unix_ms: Date.now(),
            commands,
            dashboards_flask_binding: {
                method: 'POST',
                path: '/api/dev/simulation',
                body: dashboardsBody,
            },
            smart_home_fanout: {
                target: 'integrations/smart-room/actuators',
                body: {
                    environment_id: 'bedroom-sim-01',
                    scenario: fanoutScenario,
                    commands,
                },
            },
        };
        code.textContent = JSON.stringify(intent, null, 2);
    }

    function renderSunriseSmartHomeIntent(progressPercent, lumen, kelvin) {
        const code = document.getElementById('outbound-payload-sunrise');
        if (!code) return;
        const prog = Math.min(Math.max(Number(progressPercent) || 0, 0), 100);
        const { lumenLo, lumenHi } = getSunriseRanges();
        const bpct = sunriseBrightnessPct(lumen, lumenLo, lumenHi);
        const lightingTargets = {
            luminance_lm: Math.round(Number(lumen) || 0),
            correlated_color_temperature_k: Math.round(Number(kelvin) || 0),
            transition_hint_ms: sunriseDemoActive ? 120 : 600000,
        };
        if (bpct !== null) lightingTargets.brightness_percent = bpct;
        const intent = {
            bridge: 'virtual-smart-home (intent only)',
            issued_at_unix_ms: Date.now(),
            smart_home_fanout: {
                target: 'integrations/lighting/wake/sunrise-sequence',
                body: {
                    scene: sunriseDemoActive ? 'sunrise_demo_ramp_accelerated' : 'scheduled_sunrise_live',
                    progress_percent: Math.round(prog * 10) / 10,
                    lighting_targets: lightingTargets,
                },
            },
        };
        code.textContent = JSON.stringify(intent, null, 2);
    }

    function applySunriseVisual(progressPercent, lumen, kelvin) {
        const p = Math.min(Math.max(Number(progressPercent) || 0, 0), 100);
        const { r, g, b } = sunriseRgbFromLumenKelvin(lumen, kelvin);
        const card = document.getElementById('sunrise-card');
        if (card) card.style.backgroundColor = `rgb(${r}, ${g}, ${b})`;

        const bar = document.getElementById('sunrise-progress');
        const sun = document.getElementById('sunrise-sun');
        if (bar) bar.style.width = `${p}%`;
        if (sun) sun.style.left = `${p}%`;

        const lEl = document.getElementById('sunrise-lumen');
        const kEl = document.getElementById('sunrise-color-temp');
        if (lEl) lEl.innerText = lumen;
        if (kEl) kEl.innerText = kelvin;

        if (sun) sun.style.background = `rgb(${r}, ${g}, ${b})`;

        renderSunriseSmartHomeIntent(p, lumen, kelvin);
    }

    function stopSunriseDemo() {
        if (sunriseDemoRafId !== null) {
            cancelAnimationFrame(sunriseDemoRafId);
            sunriseDemoRafId = null;
        }
        sunriseDemoActive = false;
        const btn = document.getElementById('sunrise-demo-btn');
        if (btn) btn.innerText = 'Run Demo';
    }

    function startSunriseDemo() {
        stopSunriseDemo();
        sunriseDemoActive = true;
        const btn = document.getElementById('sunrise-demo-btn');
        if (btn) btn.innerText = 'Stop Demo';

        const t0 = performance.now();
        function frame(now) {
            if (!sunriseDemoActive) return;
            const elapsed = (now - t0) / 1000;
            const u = Math.min(elapsed / SUNRISE_DEMO_SECONDS, 1);
            const progressPct = u * 100;
            const { lumen, kelvin } = mapProgressToLumenKelvin(progressPct);
            applySunriseVisual(progressPct, lumen, kelvin);
            const statusEl = document.getElementById('sunrise-status');
            if (statusEl) {
                statusEl.innerText = `Demo: accelerated ramp (${progressPct.toFixed(1)}% · 30 min in ${SUNRISE_DEMO_SECONDS}s)`;
            }
            const minEl = document.getElementById('sunrise-minutes');
            if (minEl) minEl.innerText = '-';

            if (u < 1) {
                sunriseDemoRafId = requestAnimationFrame(frame);
            } else {
                stopSunriseDemo();
                refreshSunriseSequence();
            }
        }
        sunriseDemoRafId = requestAnimationFrame(frame);
    }

    function loadSunriseRangeSettings() {
        try {
            const lm = localStorage.getItem(LS_SUNRISE.lumenMin);
            const lx = localStorage.getItem(LS_SUNRISE.lumenMax);
            const km = localStorage.getItem(LS_SUNRISE.kelvinMin);
            const kx = localStorage.getItem(LS_SUNRISE.kelvinMax);
            if (lm != null) document.getElementById('sunrise-lumen-min').value = lm;
            if (lx != null) document.getElementById('sunrise-lumen-max').value = lx;
            if (km != null) document.getElementById('sunrise-kelvin-min').value = km;
            if (kx != null) document.getElementById('sunrise-kelvin-max').value = kx;
        } catch (e) { /* ignore */ }
    }

    function persistSunriseRangeSettings() {
        try {
            localStorage.setItem(LS_SUNRISE.lumenMin, document.getElementById('sunrise-lumen-min').value);
            localStorage.setItem(LS_SUNRISE.lumenMax, document.getElementById('sunrise-lumen-max').value);
            localStorage.setItem(LS_SUNRISE.kelvinMin, document.getElementById('sunrise-kelvin-min').value);
            localStorage.setItem(LS_SUNRISE.kelvinMax, document.getElementById('sunrise-kelvin-max').value);
        } catch (e) { /* ignore */ }
    }

    function initializeSunriseCard() {
        loadSunriseRangeSettings();
        ['sunrise-lumen-min', 'sunrise-lumen-max', 'sunrise-kelvin-min', 'sunrise-kelvin-max'].forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.addEventListener('change', persistSunriseRangeSettings);
        });
        const demoBtn = document.getElementById('sunrise-demo-btn');
        if (demoBtn) {
            demoBtn.addEventListener('click', () => {
                if (sunriseDemoActive) {
                    stopSunriseDemo();
                    refreshSunriseSequence();
                } else {
                    startSunriseDemo();
                }
            });
        }
    }

    async function refreshSunriseSequence() {
        if (sunriseDemoActive) return;
        try {
            const response = await fetch('/api/simulated-room');
            const payload = await response.json();
            if (!response.ok || !payload.sunrise_sequence) {
                throw new Error(payload.error || 'No sunrise data available');
            }

            const sunrise = payload.sunrise_sequence;
            lastSunriseSequence = sunrise;

            document.getElementById('sunrise-wake-time').innerText = sunrise.wake_time || '--:--';

            updateMinutesToWakeDisplay();

            const preludeMins = getUnifiedMinutesToWake();
            if (preludeMins !== null && Number.isFinite(preludeMins)) {
                applyLocalSunrisePreludeFromMinutes(preludeMins);
                return;
            }

            const progress = Number(sunrise.progress_percent || 0);
            const { lumen, kelvin } = mapProgressToLumenKelvin(progress);

            applySunriseVisual(progress, lumen, kelvin);

            const statusText = sunrise.active
                ? `Ramping (${progress.toFixed(1)}%)`
                : sunrise.phase === 'wake_now'
                    ? 'Wake sequence complete'
                    : 'Waiting for sunrise window';
            document.getElementById('sunrise-status').innerText = statusText;
        } catch (err) {
            console.error('Failed to refresh sunrise sequence:', err);
            document.getElementById('sunrise-status').innerText = 'Unavailable';
        }
    }

    const INTERVENTION_ROW_IDLE = 'dash-intervention-row';
    const INTERVENTION_BADGE_IDLE = 'dash-intervention-badge';

    function applyInterventionBadge(rowId, badgeId, running, runningKind) {
        const row = document.getElementById(rowId);
        const badge = document.getElementById(badgeId);
        if (!row || !badge) return;

        badge.textContent = running ? 'ACTIVE' : 'IDLE';

        const marginPrefix = rowId === 'intervention-air-filtration-row' ? '' : 'mb-2 ';
        const accent = interventionAccent(runningKind);
        badge.className = running ? accent.badgeClass : INTERVENTION_BADGE_IDLE;
        row.className = running ? `${marginPrefix}${accent.rowClass}` : `${marginPrefix}${INTERVENTION_ROW_IDLE}`;
    }

    function interventionAccent(kind) {
        switch (kind) {
            case 'cooling':
                return {
                    rowClass:
                        'flex items-center justify-between rounded-2xl bg-teal-500/10 px-3 py-2.5 ring-2 ring-teal-400/35 shadow-[0_0_24px_rgba(126,242,197,0.12)] transition-all duration-300',
                    badgeClass:
                        'rounded-full bg-teal-500/25 px-3 py-1 text-[10px] font-bold uppercase tracking-widest text-teal-100 ring-1 ring-teal-400/40 shadow-[0_0_14px_rgba(126,242,197,0.18)] transition-all duration-300',
                };
            case 'heater':
                return {
                    rowClass:
                        'flex items-center justify-between rounded-2xl bg-amber-500/[0.09] px-3 py-2.5 ring-2 ring-amber-400/35 shadow-[0_0_26px_rgba(212,175,55,0.14)] transition-all duration-300',
                    badgeClass:
                        'rounded-full bg-amber-500/25 px-3 py-1 text-[10px] font-bold uppercase tracking-widest text-amber-100 ring-1 ring-amber-400/45 shadow-[0_0_14px_rgba(212,175,55,0.2)] transition-all duration-300',
                };
            case 'noise':
                return {
                    rowClass:
                        'flex items-center justify-between rounded-2xl bg-violet-500/[0.08] px-3 py-2.5 ring-2 ring-violet-400/30 shadow-[0_0_22px_rgba(167,139,250,0.12)] transition-all duration-300',
                    badgeClass:
                        'rounded-full bg-violet-500/22 px-3 py-1 text-[10px] font-bold uppercase tracking-widest text-violet-100 ring-1 ring-violet-400/35 transition-all duration-300',
                };
            case 'fan':
                return {
                    rowClass:
                        'flex items-center justify-between rounded-2xl bg-cyan-500/[0.08] px-3 py-2.5 ring-2 ring-cyan-400/35 shadow-[0_0_22px_rgba(126,242,197,0.1)] transition-all duration-300',
                    badgeClass:
                        'rounded-full bg-cyan-500/22 px-3 py-1 text-[10px] font-bold uppercase tracking-widest text-cyan-100 ring-1 ring-cyan-400/35 transition-all duration-300',
                };
            case 'air_filtration':
                return {
                    rowClass:
                        'flex items-center justify-between rounded-2xl bg-emerald-500/[0.09] px-3 py-2.5 ring-2 ring-emerald-400/35 shadow-[0_0_22px_rgba(52,211,153,0.14)] transition-all duration-300',
                    badgeClass:
                        'rounded-full bg-emerald-500/24 px-3 py-1 text-[10px] font-bold uppercase tracking-widest text-emerald-100 ring-1 ring-emerald-400/40 transition-all duration-300',
                };
            default:
                return {
                    rowClass:
                        'flex items-center justify-between rounded-2xl bg-white/[0.06] px-3 py-2.5 ring-2 ring-white/15 transition-all duration-300',
                    badgeClass:
                        'rounded-full bg-white/15 px-3 py-1 text-[10px] font-bold uppercase tracking-widest text-zinc-100 ring-1 ring-white/20 transition-all duration-300',
                };
        }
    }

    function inferActiveDevScenarioKey(devSim) {
        const d = devSim || {};
        const ft = d.force_high_temperature === true;
        const fn = d.force_high_noise === true;
        const fl = d.force_low_temperature === true;
        const fv = d.force_voc_spike === true;
        if (!ft && !fn && !fl && !fv) return 'clear';
        const n = [ft, fn, fl, fv].filter(Boolean).length;
        if (n !== 1) return null;
        if (fl) return 'cold_night';
        if (ft) return 'hot_room';
        if (fn) return 'noisy_room';
        if (fv) return 'voc_spike';
        return null;
    }

    function applyDevSimulationUi(devSim) {
        const active = inferActiveDevScenarioKey(devSim);
        document.querySelectorAll('.dev-scenario-btn').forEach((btn) => {
            const sc = btn.getAttribute('data-scenario');
            const on = (active && sc === active) || (active === 'clear' && sc === 'clear');
            btn.classList.toggle('dash-dev-scenario--active', !!on);
        });
    }

    function setDevScenarioButtonsActive(scenarioKey) {
        document.querySelectorAll('.dev-scenario-btn').forEach((btn) => {
            const sc = btn.getAttribute('data-scenario');
            btn.classList.toggle('dash-dev-scenario--active', sc === scenarioKey);
        });
    }

    async function postDevSimulationScenario(scenarioKey) {
        const msgEl = document.getElementById('dev-simulation-status-msg');
        if (msgEl) msgEl.innerText = '';
        setDevScenarioButtonsActive(scenarioKey);
        renderSimulationSmartHomeIntent({}, { pendingScenario: scenarioKey });
        try {
            const res = await fetch('/api/dev/simulation', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ scenario: scenarioKey }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.error || res.statusText);
            await refreshSystemInterventionStatus();
            if (msgEl) msgEl.innerText = '';
        } catch (e) {
            console.error(e);
            if (msgEl) msgEl.innerText = 'Test scenario update failed. Sign in and try again.';
            await refreshSystemInterventionStatus();
        }
    }

    async function refreshSystemInterventionStatus() {
        const foot = document.getElementById('intervention-status-footnote');
        try {
            const response = await fetch('/api/simulated-room');
            const payload = await response.json();
            if (!response.ok) {
                throw new Error(payload.error || 'Failed to load intervention state');
            }

            if (payload.dev_simulation) applyDevSimulationUi(payload.dev_simulation);

            const si = payload.dev_simulation || {};
            if (document.getElementById('outbound-payload-simulation')) {
                renderSimulationSmartHomeIntent(si);
            }

            const hw = payload.simulated_hardware || {};
            const cool = hw.cooling === true;
            const heat = hw.heater === true;
            const noise = hw.white_noise === true;
            const fanOn = hw.fan === true;
            const filtration = hw.air_filtration_high_fan === true;

            applyInterventionBadge(
                'intervention-cooling-row',
                'intervention-cooling-badge',
                cool,
                'cooling',
            );
            applyInterventionBadge(
                'intervention-heater-row',
                'intervention-heater-badge',
                heat,
                'heater',
            );
            applyInterventionBadge(
                'intervention-white-noise-row',
                'intervention-white-noise-badge',
                noise,
                'noise',
            );
            applyInterventionBadge(
                'intervention-fan-row',
                'intervention-fan-badge',
                fanOn,
                'fan',
            );
            applyInterventionBadge(
                'intervention-air-filtration-row',
                'intervention-air-filtration-badge',
                filtration,
                'air_filtration',
            );

            const fanIcon = document.getElementById('intervention-fan-icon');
            if (fanIcon) {
                fanIcon.style.animation = fanOn
                    ? 'fan-spin-tw 0.9s linear infinite'
                    : 'none';
            }

            if (foot) {
                const ts = payload.timestamp
                    ? parseUtcTimestamp(payload.timestamp)
                    : null;
                const shown = ts && !Number.isNaN(ts.getTime())
                    ? ts.toLocaleString()
                    : '-';
                const devS = payload.dev_simulation || {};
                const simHint = (
                    devS.force_high_temperature === true
                    || devS.force_high_noise === true
                    || devS.force_low_temperature === true
                    || devS.force_voc_spike === true
                )
                    ? ' · demo test scenario on'
                    : '';
                foot.innerText = `Last sync: ${shown}${simHint}.`;
            }
        } catch (err) {
            console.error('Failed to refresh system intervention status:', err);
            if (foot) {
                foot.innerText = 'Could not load room state.';
            }
        }
    }

    const READINESS_VAL_SCORED =
        'mt-2 text-6xl font-extrabold tracking-tight text-transparent bg-gradient-to-br from-amber-200 via-[#d4af37] to-amber-600 bg-clip-text sm:text-7xl transition-all duration-300';
    const READINESS_VAL_EMPTY =
        'mt-2 text-6xl font-extrabold tracking-tight text-zinc-600 sm:text-7xl transition-all duration-300';

    async function loadReadinessHero() {
        const valEl = document.getElementById('sleep-readiness-value');
        const subEl = document.getElementById('sleep-readiness-sub');
        if (!valEl || !subEl) return;
        try {
            const res = await fetch('/api/sleep-readiness/latest');
            const data = await res.json();
            if (!res.ok) {
                throw new Error(data.error || 'Request failed');
            }
            const sc = data.score;
            if (!sc || sc.readiness_score === null || sc.readiness_score === undefined) {
                valEl.textContent = '-';
                valEl.className = READINESS_VAL_EMPTY;
                subEl.textContent = 'No sleep score yet. Complete ASLEEP to AWAKE.';
                return;
            }
            valEl.className = READINESS_VAL_SCORED;
            valEl.textContent = String(Math.round(Number(sc.readiness_score)));
            subEl.textContent = sc.score_date
                ? `Sleep date (local): ${sc.score_date}`
                : 'Latest scored session';
        } catch (e) {
            valEl.textContent = '-';
            valEl.className = READINESS_VAL_EMPTY;
            subEl.textContent = 'Could not load sleep score';
        }
    }

    (function initDashMetricTipViewportClamp() {
        const PAD = 10;

        function resetBubble(bubble) {
            bubble.style.transform = '';
        }

        function clampDashMetricTip(tip) {
            const bubble = tip.querySelector('.dash-metric-tip__bubble');
            if (!bubble) return;
            resetBubble(bubble);
            window.requestAnimationFrame(() => {
                window.requestAnimationFrame(() => {
                    const r = bubble.getBoundingClientRect();
                    const w = window.innerWidth;
                    let dx = 0;
                    for (let i = 0; i < 8; i++) {
                        const left = r.left + dx;
                        const right = r.right + dx;
                        if (right <= w - PAD && left >= PAD) break;
                        if (right > w - PAD) dx -= right - (w - PAD);
                        else if (left < PAD) dx += PAD - left;
                    }
                    if (dx) bubble.style.transform = `translateX(calc(-50% + ${dx}px))`;
                });
            });
        }

        document.querySelectorAll('.dash-metric-tip').forEach((tip) => {
            const bubble = tip.querySelector('.dash-metric-tip__bubble');
            if (!bubble) return;
            tip.addEventListener('mouseenter', () => clampDashMetricTip(tip));
            tip.addEventListener('mouseleave', () => resetBubble(bubble));
            tip.addEventListener('focusin', () => clampDashMetricTip(tip));
            tip.addEventListener('focusout', () => resetBubble(bubble));
        });

        let scrollResizeTimer;
        function onScrollOrResize() {
            window.clearTimeout(scrollResizeTimer);
            scrollResizeTimer = window.setTimeout(() => {
                document.querySelectorAll('.dash-metric-tip:hover').forEach(clampDashMetricTip);
                const ae = document.activeElement;
                const host = ae && ae.closest ? ae.closest('.dash-metric-tip') : null;
                if (host) clampDashMetricTip(host);
            }, 40);
        }
        window.addEventListener('resize', onScrollOrResize);
        window.addEventListener('scroll', onScrollOrResize, true);
    })();

    attachComfortSliderHandlers();
    initializeConfigPanel();
    applyLiveReadingsPayload(LIVE_READINGS_INITIAL);
    initializeMorningReviewCard();
    initializeSunriseCard();
    (function bindWakeMinutesPreview() {
        const wt = document.getElementById('cfg-wake-time');
        if (!wt) return;
        let idle;
        const bump = () => {
            window.clearTimeout(idle);
            idle = window.setTimeout(() => updateMinutesToWakeDisplay(), 80);
        };
        wt.addEventListener('input', bump);
        wt.addEventListener('change', bump);
    })();
    loadReadinessHero();
    setInterval(loadReadinessHero, 60000);
    loadSleepScoreHistoryChart();
    setInterval(loadSleepScoreHistoryChart, 300000);
    refreshSunriseSequence();
    setTimeout(() => tickSunriseRealtimePrelude(), 0);
    refreshSystemInterventionStatus();
    setInterval(refreshSunriseSequence, 10000);
    setInterval(tickSunriseRealtimePrelude, 60000);
    setInterval(refreshSystemInterventionStatus, 5000);
    (function initDevSimulationScenarios() {
        document.querySelectorAll('.dev-scenario-btn').forEach((btn) => {
            btn.addEventListener('click', () => {
                const sc = btn.getAttribute('data-scenario');
                if (sc) postDevSimulationScenario(sc);
            });
        });
    })();

    (function initSleepCoachChat() {
        const fab = document.getElementById('sleep-coach-fab');
        const panel = document.getElementById('sleep-coach-panel');
        const closeBtn = document.getElementById('sleep-coach-close');
        const messagesEl = document.getElementById('sleep-coach-messages');
        const inputEl = document.getElementById('sleep-coach-input');
        const sendBtn = document.getElementById('sleep-coach-send');
        if (!fab || !panel || !messagesEl || !inputEl || !sendBtn) return;

        function scrollSleepCoachToBottom() {
            window.requestAnimationFrame(() => {
                messagesEl.scrollTop = messagesEl.scrollHeight;
            });
        }

        function sleepCoachMarkdownToHtml(src) {
            const raw = String(src ?? '');
            try {
                if (typeof marked !== 'undefined' && marked != null) {
                    const parsed = typeof marked.parse === 'function'
                        ? marked.parse(raw)
                        : (typeof marked === 'function' ? marked(raw) : '');
                    if (typeof parsed === 'string' && parsed.trim()) {
                        return parsed;
                    }
                }
            } catch (e) {
                console.warn('Sleep Coach markdown parse failed:', e);
            }
            const esc = document.createElement('div');
            esc.textContent = raw;
            return `<p class="my-1">${esc.innerHTML}</p>`;
        }

        function appendMessage(sender, text) {
            const wrap = document.createElement('div');
            wrap.className = sender === 'user' ? 'flex justify-end' : 'flex justify-start';

            const userGlow =
                'border border-teal-400/40 bg-gradient-to-br from-teal-500/25 '
                + 'via-teal-300/18 to-amber-500/15 text-zinc-100 '
                + 'shadow-[0_0_20px_rgba(126,242,197,0.12)] ring-1 ring-teal-400/25';
            const aiMuted =
                'border border-zinc-600/45 bg-zinc-800/90 shadow-inner shadow-black/30';

            const bubble = document.createElement('div');

            if (sender === 'user') {
                bubble.className =
                    `max-w-[85%] break-words rounded-2xl px-3 py-2 text-sm leading-relaxed whitespace-pre-wrap ${userGlow}`;
                bubble.textContent = text;
            } else {
                bubble.className =
                    `max-w-[85%] min-w-0 break-words rounded-2xl px-3 py-2 ${aiMuted}`;
                const prose = document.createElement('div');
                prose.className =
                    'prose prose-invert prose-sm max-w-none text-gray-200';
                prose.innerHTML = sleepCoachMarkdownToHtml(text);
                bubble.appendChild(prose);
            }

            wrap.appendChild(bubble);
            messagesEl.appendChild(wrap);
            scrollSleepCoachToBottom();
        }

        function removeThinkingIndicator() {
            document.getElementById('sleep-coach-thinking')?.remove();
        }

        function showThinkingIndicator() {
            removeThinkingIndicator();
            const wrap = document.createElement('div');
            wrap.id = 'sleep-coach-thinking';
            wrap.className = 'flex justify-start';
            const bubble = document.createElement('div');
            bubble.className =
                'max-w-[85%] rounded-2xl border border-white/10 bg-zinc-900/65 px-3 '
                + 'py-2 text-sm italic text-zinc-500';
            bubble.textContent = 'Thinking...';
            wrap.appendChild(bubble);
            messagesEl.appendChild(wrap);
            scrollSleepCoachToBottom();
        }

        function setCoachOpen(open) {
            panel.classList.toggle('hidden', !open);
            fab.setAttribute('aria-expanded', open ? 'true' : 'false');
            fab.setAttribute(
                'aria-label',
                open ? 'Close Sleep Coach' : 'Open Sleep Coach',
            );
            if (open) {
                window.setTimeout(() => inputEl.focus(), 100);
                scrollSleepCoachToBottom();
            }
        }

        fab.addEventListener('click', () => {
            setCoachOpen(panel.classList.contains('hidden'));
        });
        closeBtn?.addEventListener('click', () => setCoachOpen(false));

        async function sendMessage() {
            const text = String(inputEl.value || '').trim();
            if (!text) return;

            appendMessage('user', text);
            inputEl.value = '';
            showThinkingIndicator();
            sendBtn.disabled = true;

            try {
                const response = await fetch('/api/sleep-coach', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'same-origin',
                    body: JSON.stringify({ message: text }),
                });
                let data = {};
                try {
                    data = await response.json();
                } catch (parseErr) {
                    data = {};
                }
                removeThinkingIndicator();

                if (!response.ok) {
                    const err =
                        (typeof data.error === 'string' && data.error)
                        || `${response.status} ${response.statusText || 'Request failed'}`;
                    appendMessage('assistant', err);
                    return;
                }
                const rec = data.recommendation;
                appendMessage(
                    'assistant',
                    rec && String(rec).trim()
                        ? String(rec).trim()
                        : '(No recommendation returned.)',
                );
            } catch (err) {
                removeThinkingIndicator();
                appendMessage(
                    'assistant',
                    err && err.message
                        ? err.message
                        : 'Network error. Check your connection and try again.',
                );
            } finally {
                sendBtn.disabled = false;
                scrollSleepCoachToBottom();
            }
        }

        sendBtn.addEventListener('click', sendMessage);
        inputEl.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        });
    })();
