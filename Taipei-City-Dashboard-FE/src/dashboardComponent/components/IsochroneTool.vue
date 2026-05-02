<script setup>
import { ref } from "vue";
import { useMapStore } from "../../store/mapStore";

const props = defineProps([
	"active_chart",
	"chart_config",
	"series",
	"map_config",
	"map_filter",
	"map_filter_on",
]);

const mapStore = useMapStore();

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

// Use colors from chart_config if available, otherwise defaults
const colors = props.chart_config?.color ?? [
	"#2ecc71",
	"#f1c40f",
	"#e67e22",
	"#e74c3c",
];
// Strip leading # so they can be passed to the API
const hexCodes = colors.map((c) => c.replace("#", ""));

const selectedOrigin = ref(PRESET_ORIGINS[0]);
const selectedProfile = ref("driving-traffic");
const selectedMinutes = ref([15, 30, 45, 60]);
const isPicking = ref(false);

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

function calculate() {
	mapStore.addIsochroneOverlay({
		lng: selectedOrigin.value.lng,
		lat: selectedOrigin.value.lat,
		profile: selectedProfile.value,
		minutes: selectedMinutes.value,
		colors: hexCodes.slice(0, selectedMinutes.value.length),
	});
}

function clear() {
	mapStore.removeIsochroneOverlay();
}
</script>

<template>
  <div class="isochronetool">
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
        <span>{{ isPicking ? 'close' : 'my_location' }}</span>
      </button>
    </div>

    <p
      v-if="isPicking"
      class="isochronetool-hint"
    >
      請在地圖上點擊起點位置
    </p>

    <!-- 模式 + 時間 -->
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
        @click="clear"
      >
        清除
      </button>
      <button
        class="isochronetool-actions-calc"
        :disabled="mapStore.isochroneState.isLoading || isPicking"
        @click="calculate"
      >
        {{ mapStore.isochroneState.isLoading ? '計算中...' : '計算通勤圈' }}
      </button>
    </div>

    <!-- 圖例 -->
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
</template>

<style scoped lang="scss">
* {
	margin: 0;
	padding: 0;
	box-sizing: border-box;
}

.isochronetool {
	width: 100%;
	height: 100%;
	display: flex;
	flex-direction: column;
	gap: 6px;
	padding: 4px 2px;
	overflow-y: auto;
	font-family: "微軟正黑體", "Microsoft JhengHei", "Droid Sans", sans-serif;

	&::-webkit-scrollbar { width: 3px; }
	&::-webkit-scrollbar-thumb { border-radius: 3px; background: var(--color-border); }

	&-row {
		display: flex;
		align-items: center;
		gap: 6px;
	}

	&-label {
		font-size: 0.7rem;
		color: var(--color-complement-text);
		width: 2rem;
		flex-shrink: 0;
	}

	&-select {
		flex: 1;
		min-width: 0;
		padding: 2px 4px;
		border-radius: 4px;
		border: 1px solid var(--color-border);
		background: var(--color-component-background);
		color: var(--color-normal-text);
		font-size: 0.7rem;
		cursor: pointer;
	}

	&-custom {
		flex: 1;
		font-size: 0.7rem;
		color: var(--color-highlight);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	&-pick {
		width: 1.4rem;
		height: 1.4rem;
		display: flex;
		align-items: center;
		justify-content: center;
		border-radius: 4px;
		border: 1px solid var(--color-border);
		background: transparent;
		color: var(--color-complement-text);
		flex-shrink: 0;
		cursor: pointer;
		transition: color 0.2s, border-color 0.2s;

		span {
			font-family: "Material Icons Round";
			font-size: 0.85rem;
		}

		&:hover, &.picking {
			color: var(--color-highlight);
			border-color: var(--color-highlight);
		}
	}

	&-hint {
		font-size: 0.65rem;
		color: var(--color-highlight);
		padding-left: 2.5rem;
		animation: blink 1s ease-in-out infinite;
	}

	&-profiles {
		display: flex;
		gap: 4px;

		button {
			padding: 1px 6px;
			border-radius: 4px;
			border: 1px solid var(--color-border);
			background: transparent;
			color: var(--color-complement-text);
			font-size: 0.75rem;
			cursor: pointer;
			transition: background 0.15s;

			&.active {
				background: var(--color-highlight);
				border-color: var(--color-highlight);
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
			border-radius: 50%;
			border: 1px solid var(--color-border);
			color: var(--color-complement-text);
			font-size: 0.65rem;
			cursor: pointer;
			transition: background 0.15s, border-color 0.15s;

			&:hover { border-color: var(--color-highlight); }
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
			cursor: pointer;
			transition: opacity 0.2s;

			&:hover { opacity: 0.8; }
			&:disabled { opacity: 0.4; cursor: not-allowed; }
		}

		&-clear {
			background: transparent;
			border: 1px solid var(--color-border);
			color: var(--color-complement-text);
		}

		&-calc {
			background: var(--color-highlight);
			border: none;
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
			font-size: 0.65rem;
			color: var(--color-complement-text);
		}

		&-swatch {
			width: 8px;
			height: 8px;
			border-radius: 2px;
			flex-shrink: 0;
		}
	}
}

@keyframes blink {
	0%, 100% { opacity: 1; }
	50% { opacity: 0.4; }
}
</style>
