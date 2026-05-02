<script setup>
import { ref, computed, onMounted } from "vue";
import { useMapStore } from "../../store/mapStore";

// ── Store ──────────────────────────────────────────────────────────────────
const mapStore = useMapStore();

// ── Props ──────────────────────────────────────────────────────────────────
const props = defineProps([
	"active_chart",
	"chart_config",
	"series",
	"map_config",
	"map_filter",
	"map_filter_on",
]);

// ── Module-level constants ─────────────────────────────────────────────────
const PRESET_ORIGINS = [
	{ label: "臺北車站", lng: 121.517, lat: 25.0478 },
	{ label: "板橋車站", lng: 121.4637, lat: 25.0144 },
	{ label: "臺北市政府", lng: 121.5654, lat: 25.0376 },
	{ label: "新北市政府", lng: 121.4636, lat: 25.0123 },
	{ label: "南港車站", lng: 121.6074, lat: 25.053 },
];

const PROFILES = [
	{ value: "driving-traffic", label: "🚗" },
	{ value: "cycling", label: "🚲" },
	{ value: "walking", label: "🚶" },
];

const SVG_W = 300;
const SVG_H = 200;
const PREVIEW_ORIGIN = { lng: 121.517, lat: 25.0478, label: "臺北車站" };
const PREVIEW_MINUTES = [60, 45, 30, 15];

const colors = props.chart_config?.color ?? [
	"#2ecc71",
	"#f1c40f",
	"#e67e22",
	"#e74c3c",
];
const hexCodes = colors.map((c) => c.replace("#", ""));

// ── Local data ─────────────────────────────────────────────────────────────
const selectedOrigin = ref(PRESET_ORIGINS[0]);
const selectedProfile = ref("driving-traffic");
const selectedMinutes = ref([15, 30, 45, 60]);
const isPicking = ref(false);

const svgPaths = ref([]);
const svgOriginPt = ref(null);
const previewLoading = ref(true);
const previewError = ref(false);

// ── Computed ───────────────────────────────────────────────────────────────
// Show interactive controls when Mapbox map is mounted; otherwise show SVG preview
const mapReady = computed(() => !!mapStore.map);

// ── Methods ────────────────────────────────────────────────────────────────
function pickOnMap() {
	isPicking.value = true;
	mapStore.startPickingOrigin(({ lng, lat }) => {
		selectedOrigin.value = {
			label: `${lat.toFixed(4)}, ${lng.toFixed(4)}`,
			lng,
			lat,
			isCustom: true,
		};
		isPicking.value = false;
	});
}

function cancelPick() {
	isPicking.value = false;
	mapStore.cancelPickingOrigin();
}

function toggleMinute(m) {
	if (selectedMinutes.value.includes(m)) {
		if (selectedMinutes.value.length > 1)
			selectedMinutes.value = selectedMinutes.value.filter((x) => x !== m);
	} else {
		selectedMinutes.value = [...selectedMinutes.value, m].sort((a, b) => a - b);
	}
}

function handleCalculate() {
	mapStore.addIsochroneOverlay({
		lng: selectedOrigin.value.lng,
		lat: selectedOrigin.value.lat,
		profile: selectedProfile.value,
		minutes: selectedMinutes.value,
		colors: hexCodes.slice(0, selectedMinutes.value.length),
	});
}

function clearOverlay() {
	mapStore.removeIsochroneOverlay();
}

function buildBounds(features) {
	let minLng = Infinity, maxLng = -Infinity;
	let minLat = Infinity, maxLat = -Infinity;
	features.forEach((f) => {
		const rings =
			f.geometry.type === "Polygon"
				? f.geometry.coordinates
				: f.geometry.coordinates.flat(1);
		rings.forEach((ring) =>
			ring.forEach(([lng, lat]) => {
				if (lng < minLng) minLng = lng;
				if (lng > maxLng) maxLng = lng;
				if (lat < minLat) minLat = lat;
				if (lat > maxLat) maxLat = lat;
			})
		);
	});
	const dLng = (maxLng - minLng) * 0.06;
	const dLat = (maxLat - minLat) * 0.06;
	const cosLat = Math.cos(((minLat + maxLat) / 2) * (Math.PI / 180));
	return {
		minLng: minLng - dLng,
		maxLng: maxLng + dLng,
		minLat: minLat - dLat,
		maxLat: maxLat + dLat,
		cosLat,
	};
}

function projectCoord(lng, lat, bounds) {
	const lngSpan = (bounds.maxLng - bounds.minLng) * bounds.cosLat;
	const latSpan = bounds.maxLat - bounds.minLat;
	let scaleX, scaleY, offX = 0, offY = 0;
	if (lngSpan / latSpan > SVG_W / SVG_H) {
		scaleX = SVG_W / lngSpan;
		scaleY = scaleX;
		offY = (SVG_H - latSpan * scaleY) / 2;
	} else {
		scaleY = SVG_H / latSpan;
		scaleX = scaleY;
		offX = (SVG_W - lngSpan * scaleX) / 2;
	}
	return {
		x: offX + (lng - bounds.minLng) * bounds.cosLat * scaleX,
		y: offY + (bounds.maxLat - lat) * scaleY,
	};
}

function ringToPath(ring, bounds) {
	return (
		ring
			.map(([lng, lat], i) => {
				const { x, y } = projectCoord(lng, lat, bounds);
				return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
			})
			.join(" ") + " Z"
	);
}

function featureToD(feature, bounds) {
	const rings =
		feature.geometry.type === "Polygon"
			? feature.geometry.coordinates
			: feature.geometry.coordinates.flat(1);
	return rings.map((r) => ringToPath(r, bounds)).join(" ");
}

async function loadPreview() {
	previewLoading.value = true;
	previewError.value = false;
	try {
		const url =
			`/api/dev/isochrone/?profile=driving-traffic` +
			`&lng=${PREVIEW_ORIGIN.lng}&lat=${PREVIEW_ORIGIN.lat}` +
			`&minutes=${PREVIEW_MINUTES.join(",")}` +
			`&colors=${hexCodes.slice(0, PREVIEW_MINUTES.length).join(",")}`;
		const res = await fetch(url);
		if (!res.ok) throw new Error(`HTTP ${res.status}`);
		const geojson = await res.json();
		if (!geojson?.features?.length) throw new Error("empty");

		const bounds = buildBounds(geojson.features);
		const sorted = [...geojson.features].sort(
			(a, b) => (b.properties.contour ?? 0) - (a.properties.contour ?? 0)
		);
		svgPaths.value = sorted.map((f) => {
			const minute = f.properties.contour;
			const idx = PREVIEW_MINUTES.indexOf(minute);
			return {
				d: featureToD(f, bounds),
				color: colors[idx >= 0 ? idx : 0],
				minute,
			};
		});
		svgOriginPt.value = projectCoord(
			PREVIEW_ORIGIN.lng,
			PREVIEW_ORIGIN.lat,
			bounds
		);
	} catch {
		previewError.value = true;
	} finally {
		previewLoading.value = false;
	}
}

// ── Lifecycle ──────────────────────────────────────────────────────────────
onMounted(loadPreview);
</script>

<template>
  <!-- Map view: interactive controls -->
  <div
    v-if="mapReady"
    class="isochronetool"
  >
    <!-- 起點 -->
    <div class="isochronetool-row">
      <span class="isochronetool-label">起點</span>
      <select
        v-if="!selectedOrigin.isCustom"
        v-model="selectedOrigin"
        class="isochronetool-select"
      >
        <option
          v-for="o in PRESET_ORIGINS"
          :key="o.label"
          :value="o"
        >
          {{ o.label }}
        </option>
      </select>
      <span
        v-else
        class="isochronetool-custom"
      >{{ selectedOrigin.label }}</span>
      <button
        :class="['isochronetool-pick', { picking: isPicking }]"
        :title="isPicking ? '取消點選' : '在地圖上點選'"
        @click="isPicking ? cancelPick() : pickOnMap()"
      >
        <span>{{ isPicking ? "close" : "my_location" }}</span>
      </button>
    </div>

    <p
      v-if="isPicking"
      class="isochronetool-hint"
    >
      請在地圖上點擊起點位置
    </p>

    <!-- 模式 -->
    <div class="isochronetool-row">
      <span class="isochronetool-label">模式</span>
      <div class="isochronetool-profiles">
        <button
          v-for="p in PROFILES"
          :key="p.value"
          :class="{ active: selectedProfile === p.value }"
          @click="selectedProfile = p.value"
        >
          {{ p.label }}
        </button>
      </div>
    </div>

    <!-- 時間 -->
    <div class="isochronetool-row">
      <span class="isochronetool-label">時間</span>
      <div class="isochronetool-chips">
        <span
          v-for="(m, i) in [15, 30, 45, 60]"
          :key="m"
          :class="{ active: selectedMinutes.includes(m) }"
          :style="
            selectedMinutes.includes(m)
              ? { background: colors[i], color: '#111', borderColor: colors[i] }
              : {}
          "
          @click="toggleMinute(m)"
        >{{ m }}</span>
      </div>
    </div>

    <!-- 操作 -->
    <div class="isochronetool-actions">
      <button
        class="isochronetool-actions-clear"
        @click="clearOverlay"
      >
        清除
      </button>
      <button
        class="isochronetool-actions-calc"
        :disabled="mapStore.isochroneState.isLoading || isPicking"
        @click="handleCalculate"
      >
        {{ mapStore.isochroneState.isLoading ? "計算中..." : "計算通勤圈" }}
      </button>
    </div>

    <!-- 圖例（疊圖後顯示） -->
    <div
      v-if="mapStore.isochroneState.visible"
      class="isochronetool-legend"
    >
      <div
        v-for="(m, i) in selectedMinutes"
        :key="m"
        class="isochronetool-legend-item"
      >
        <span
          class="isochronetool-legend-swatch"
          :style="{ background: colors[i] }"
        />
        {{ m }} 分鐘
      </div>
    </div>
  </div>

  <!-- General dashboard: static SVG preview -->
  <div
    v-else
    class="isochronetool-preview"
  >
    <div
      v-if="previewLoading"
      class="isochronetool-preview-state"
    >
      載入中...
    </div>
    <div
      v-else-if="previewError"
      class="isochronetool-preview-state isochronetool-preview-state-error"
    >
      <span>location_off</span>
      <p>無法取得資料</p>
    </div>
    <template v-else>
      <svg
        :viewBox="`0 0 ${SVG_W} ${SVG_H}`"
        class="isochronetool-preview-svg"
        xmlns="http://www.w3.org/2000/svg"
      >
        <path
          v-for="(f, i) in svgPaths"
          :key="i"
          :d="f.d"
          :fill="f.color"
          fill-opacity="0.4"
          :stroke="f.color"
          stroke-width="0.8"
          stroke-opacity="0.9"
          fill-rule="evenodd"
        />
        <circle
          v-if="svgOriginPt"
          :cx="svgOriginPt.x"
          :cy="svgOriginPt.y"
          r="4"
          fill="white"
          stroke="#444"
          stroke-width="1.2"
        />
      </svg>
      <div class="isochronetool-preview-legend">
        <span class="isochronetool-preview-legend-origin">
          {{ PREVIEW_ORIGIN.label }}・開車
        </span>
        <div class="isochronetool-preview-legend-items">
          <span
            v-for="(f, i) in [...svgPaths].reverse()"
            :key="i"
            class="isochronetool-preview-legend-item"
          >
            <span
              class="isochronetool-preview-legend-swatch"
              :style="{ background: f.color }"
            />{{ f.minute }}分
          </span>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped lang="scss">
.isochronetool {
	width: 100%;
	height: 100%;
	display: flex;
	flex-direction: column;
	gap: 6px;
	padding: 4px 2px;
	font-family: "微軟正黑體", "Microsoft JhengHei", "Droid Sans", sans-serif;
	overflow-y: auto;

	&::-webkit-scrollbar {
		width: 3px;
	}

	&::-webkit-scrollbar-thumb {
		border-radius: 3px;
		background: var(--color-border);
	}

	&-row {
		display: flex;
		align-items: center;
		gap: 6px;
	}

	&-label {
		width: 2rem;
		flex-shrink: 0;
		color: var(--color-complement-text);
		font-size: 0.7rem;
	}

	&-select {
		flex: 1;
		min-width: 0;
		padding: 2px 4px;
		border: 1px solid var(--color-border);
		border-radius: 4px;
		background: var(--color-component-background);
		color: var(--color-normal-text);
		font-size: 0.7rem;
		cursor: pointer;
	}

	&-custom {
		flex: 1;
		color: var(--color-highlight);
		font-size: 0.7rem;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	&-pick {
		width: 1.4rem;
		height: 1.4rem;
		display: flex;
		flex-shrink: 0;
		align-items: center;
		justify-content: center;
		border: 1px solid var(--color-border);
		border-radius: 4px;
		background: transparent;
		color: var(--color-complement-text);
		transition: color 0.2s, border-color 0.2s;
		cursor: pointer;

		span {
			font-family: "Material Icons Round";
			font-size: 0.85rem;
		}

		&:hover,
		&.picking {
			border-color: var(--color-highlight);
			color: var(--color-highlight);
		}
	}

	&-hint {
		padding-left: 2.5rem;
		color: var(--color-highlight);
		font-size: 0.65rem;
		animation: blink 1s ease-in-out infinite;
	}

	&-profiles {
		display: flex;
		gap: 4px;

		button {
			padding: 1px 6px;
			border: 1px solid var(--color-border);
			border-radius: 4px;
			background: transparent;
			color: var(--color-complement-text);
			font-size: 0.75rem;
			transition: background 0.15s;
			cursor: pointer;

			&.active {
				border-color: var(--color-highlight);
				background: var(--color-highlight);
				color: white;
			}
		}
	}

	&-chips {
		display: flex;
		gap: 4px;

		span {
			width: 1.6rem;
			height: 1.6rem;
			display: flex;
			align-items: center;
			justify-content: center;
			border: 1px solid var(--color-border);
			border-radius: 50%;
			color: var(--color-complement-text);
			font-size: 0.65rem;
			transition: background 0.15s, border-color 0.15s;
			cursor: pointer;

			&:hover {
				border-color: var(--color-highlight);
			}
		}
	}

	&-actions {
		display: flex;
		justify-content: flex-end;
		gap: 5px;

		button {
			padding: 3px 8px;
			border-radius: 4px;
			font-size: 0.7rem;
			transition: opacity 0.2s;
			cursor: pointer;

			&:hover {
				opacity: 0.8;
			}

			&:disabled {
				opacity: 0.4;
				cursor: not-allowed;
			}
		}

		&-clear {
			border: 1px solid var(--color-border);
			background: transparent;
			color: var(--color-complement-text);
		}

		&-calc {
			border: none;
			background: var(--color-highlight);
			color: white;
		}
	}

	&-legend {
		display: flex;
		flex-wrap: wrap;
		gap: 4px 8px;
		padding-top: 4px;
		border-top: 1px solid var(--color-border);

		&-item {
			display: flex;
			align-items: center;
			gap: 4px;
			color: var(--color-complement-text);
			font-size: 0.65rem;
		}

		&-swatch {
			width: 8px;
			height: 8px;
			flex-shrink: 0;
			border-radius: 2px;
		}
	}

	// Preview mode (general dashboard, no map)
	&-preview {
		width: 100%;
		height: 100%;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 6px;
		padding: 4px;

		&-state {
			display: flex;
			flex-direction: column;
			align-items: center;
			gap: 4px;
			color: var(--color-complement-text);
			font-size: 0.72rem;

			span {
				font-family: "Material Icons Round";
				font-size: 1.4rem;
			}

			&-error p {
				font-size: 0.68rem;
			}
		}

		&-svg {
			width: 100%;
			flex: 1;
			min-height: 0;
			display: block;
			border-radius: 4px;
			background: var(--color-component-background);
		}

		&-legend {
			display: flex;
			flex-shrink: 0;
			align-items: center;
			justify-content: space-between;
			gap: 6px;
			width: 100%;

			&-origin {
				color: var(--color-complement-text);
				font-size: 0.62rem;
				white-space: nowrap;
			}

			&-items {
				display: flex;
				gap: 5px;
			}

			&-item {
				display: flex;
				align-items: center;
				gap: 2px;
				color: var(--color-complement-text);
				font-size: 0.62rem;
			}

			&-swatch {
				width: 7px;
				height: 7px;
				flex-shrink: 0;
				border-radius: 2px;
			}
		}
	}
}

@keyframes blink {
	0%,
	100% {
		opacity: 1;
	}

	50% {
		opacity: 0.4;
	}
}
</style>
