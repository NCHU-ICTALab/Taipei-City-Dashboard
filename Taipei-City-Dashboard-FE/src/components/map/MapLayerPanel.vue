<script setup>
import { ref, computed } from "vue";
import { useMapStore } from "../../store/mapStore";

const mapStore = useMapStore();

/* ── 圖層清單 ── */
// 把 currentLayers 按 title 分組，跳過 isochrone 自己的圖層
const layerGroups = computed(() => {
	const groups = {};
	mapStore.currentLayers
		.filter((id) => !id.startsWith("isochrone-"))
		.forEach((id) => {
			const cfg = mapStore.mapConfigs[id];
			if (!cfg) return;
			const title = cfg.title || id;
			if (!groups[title]) groups[title] = { title, ids: [], visible: false };
			groups[title].ids.push(id);
			if (mapStore.currentVisibleLayers.includes(id))
				groups[title].visible = true;
		});
	return Object.values(groups);
});

function toggleGroup(group) {
	const vis = group.visible ? "none" : "visible";
	group.ids.forEach((id) => mapStore.toggleLayerById(id, vis));
}

/* ── 通勤圈工具 ── */
const PRESET_ORIGINS = [
	{ label: "臺北車站", lng: 121.517, lat: 25.0478 },
	{ label: "板橋車站", lng: 121.4637, lat: 25.0144 },
	{ label: "臺北市政府", lng: 121.5654, lat: 25.0376 },
	{ label: "新北市政府", lng: 121.4636, lat: 25.0123 },
	{ label: "南港車站", lng: 121.6074, lat: 25.053 },
];

const PROFILES = [
	{ value: "driving-traffic", label: "🚗 開車" },
	{ value: "cycling", label: "🚲 腳踏車" },
	{ value: "walking", label: "🚶 步行" },
];

const CONTOUR_COLORS = ["2ecc71", "f1c40f", "e67e22", "e74c3c"];

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
	mapStore.isochroneState.origin = selectedOrigin.value;
	mapStore.addIsochroneOverlay({
		lng: selectedOrigin.value.lng,
		lat: selectedOrigin.value.lat,
		profile: selectedProfile.value,
		minutes: selectedMinutes.value,
		colors: CONTOUR_COLORS.slice(0, selectedMinutes.value.length),
	});
}

function clear() {
	mapStore.removeIsochroneOverlay();
}
</script>

<template>
  <div class="maplayerpanel">
    <!-- 圖層清單 -->
    <p class="maplayerpanel-title">
      圖層
    </p>
    <div
      v-if="layerGroups.length > 0"
      class="maplayerpanel-layers"
    >
      <div
        v-for="group in layerGroups"
        :key="group.title"
        class="maplayerpanel-layers-row"
      >
        <span class="maplayerpanel-layers-name">{{ group.title }}</span>
        <button
          :class="['maplayerpanel-layers-eye', { active: group.visible }]"
          :title="group.visible ? '隱藏' : '顯示'"
          @click="toggleGroup(group)"
        >
          <span>{{ group.visible ? 'visibility' : 'visibility_off' }}</span>
        </button>
      </div>
    </div>
    <p
      v-else
      class="maplayerpanel-empty"
    >
      尚無開啟的圖層
    </p>

    <div class="maplayerpanel-divider" />

    <!-- 通勤圈工具 -->
    <p class="maplayerpanel-title">
      ✨ 通勤圈分析
    </p>

    <!-- 起點 -->
    <div class="maplayerpanel-section">
      <label>起點</label>
      <div class="maplayerpanel-origin">
        <select
          v-if="!selectedOrigin.isCustom"
          v-model="selectedOrigin"
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
          class="maplayerpanel-origin-custom"
        >{{ selectedOrigin.label }}</span>
        <button
          v-if="!isPicking"
          class="maplayerpanel-origin-pick"
          title="在地圖上點選"
          @click="pickOnMap"
        >
          <span>my_location</span>
        </button>
        <button
          v-else
          class="maplayerpanel-origin-pick picking"
          title="取消點選"
          @click="cancelPick"
        >
          <span>close</span>
        </button>
      </div>
      <p
        v-if="isPicking"
        class="maplayerpanel-picking-hint"
      >
        請在地圖上點擊起點位置
      </p>
    </div>

    <!-- 交通模式 -->
    <div class="maplayerpanel-section">
      <label>模式</label>
      <div class="maplayerpanel-profiles">
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
    <div class="maplayerpanel-section">
      <label>時間（分鐘）</label>
      <div class="maplayerpanel-chips">
        <span
          v-for="(m, i) in [15, 30, 45, 60]"
          :key="m"
          :class="{ active: selectedMinutes.includes(m) }"
          :style="
            selectedMinutes.includes(m)
              ? { background: '#' + CONTOUR_COLORS[i], color: '#111' }
              : {}
          "
          @click="toggleMinute(m)"
        >{{ m }}</span>
      </div>
    </div>

    <!-- 操作 -->
    <div class="maplayerpanel-actions">
      <button
        class="maplayerpanel-actions-clear"
        @click="clear"
      >
        清除
      </button>
      <button
        class="maplayerpanel-actions-calc"
        :disabled="mapStore.isochroneState.isLoading || isPicking"
        @click="calculate"
      >
        {{ mapStore.isochroneState.isLoading ? '計算中...' : '計算通勤圈' }}
      </button>
    </div>

    <!-- 圖例 -->
    <div
      v-if="mapStore.isochroneState.visible"
      class="maplayerpanel-legend"
    >
      <div
        v-for="(m, i) in selectedMinutes"
        :key="m"
        class="maplayerpanel-legend-item"
      >
        <span
          class="maplayerpanel-legend-swatch"
          :style="{ background: '#' + CONTOUR_COLORS[i] }"
        />
        {{ m }} 分鐘以內
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.maplayerpanel {
	position: absolute;
	right: 52px;
	top: 150px;
	width: 230px;
	max-height: calc(100% - 200px);
	overflow-y: auto;
	padding: 12px;
	border: 1px solid var(--color-border);
	border-radius: 8px;
	background-color: var(--color-component-background);
	z-index: 2;
	box-shadow: 0 2px 12px rgba(0, 0, 0, 0.4);

	&::-webkit-scrollbar {
		width: 4px;
	}
	&::-webkit-scrollbar-thumb {
		border-radius: 4px;
		background-color: var(--color-border);
	}

	&-title {
		font-size: var(--font-s);
		color: var(--color-complement-text);
		margin-bottom: 8px;
		font-weight: 600;
		letter-spacing: 0.05em;
	}

	&-empty {
		font-size: var(--font-s);
		color: var(--color-complement-text);
		opacity: 0.5;
		margin-bottom: 4px;
	}

	&-divider {
		border-top: 1px solid var(--color-border);
		margin: 10px 0;
	}

	&-layers {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin-bottom: 4px;

		&-row {
			display: flex;
			align-items: center;
			justify-content: space-between;
			padding: 3px 0;
		}

		&-name {
			font-size: var(--font-s);
			color: var(--color-normal-text);
			flex: 1;
			white-space: nowrap;
			overflow: hidden;
			text-overflow: ellipsis;
		}

		&-eye {
			width: 1.4rem;
			height: 1.4rem;
			display: flex;
			align-items: center;
			justify-content: center;
			border-radius: 4px;
			background: transparent;
			color: var(--color-complement-text);
			flex-shrink: 0;
			transition: color 0.2s;

			span {
				font-family: var(--font-icon);
				font-size: 1rem;
			}

			&.active {
				color: var(--color-highlight);
			}

			&:hover {
				color: var(--color-highlight);
			}
		}
	}

	&-section {
		display: flex;
		flex-direction: column;
		margin-bottom: 10px;

		label {
			font-size: var(--font-s);
			color: var(--color-complement-text);
			margin-bottom: 5px;
		}
	}

	&-origin {
		display: flex;
		align-items: center;
		gap: 6px;

		select {
			flex: 1;
			min-width: 0;
			padding: 3px 6px;
			border-radius: 5px;
			border: 1px solid var(--color-border);
			background-color: var(--color-component-background);
			color: var(--color-normal-text);
			font-size: var(--font-s);
			cursor: pointer;
		}

		&-custom {
			flex: 1;
			font-size: var(--font-s);
			color: var(--color-highlight);
			white-space: nowrap;
			overflow: hidden;
			text-overflow: ellipsis;
		}

		&-pick {
			width: 1.6rem;
			height: 1.6rem;
			display: flex;
			align-items: center;
			justify-content: center;
			border-radius: 4px;
			border: 1px solid var(--color-border);
			background: transparent;
			color: var(--color-complement-text);
			flex-shrink: 0;
			transition: color 0.2s, border-color 0.2s;

			span {
				font-family: var(--font-icon);
				font-size: 0.95rem;
			}

			&:hover,
			&.picking {
				color: var(--color-highlight);
				border-color: var(--color-highlight);
			}
		}
	}

	&-picking-hint {
		margin-top: 4px;
		font-size: var(--font-s);
		color: var(--color-highlight);
		animation: blink 1s ease-in-out infinite;
	}

	&-profiles {
		display: flex;
		flex-wrap: wrap;
		gap: 5px;

		button {
			padding: 2px 7px;
			border-radius: 5px;
			border: 1px solid var(--color-border);
			background: transparent;
			color: var(--color-complement-text);
			font-size: var(--font-s);
			cursor: pointer;
			transition: background-color 0.2s, color 0.2s;

			&.active {
				background-color: var(--color-highlight);
				color: white;
				border-color: var(--color-highlight);
			}
		}
	}

	&-chips {
		display: flex;
		gap: 6px;

		span {
			width: 2rem;
			height: 2rem;
			display: flex;
			align-items: center;
			justify-content: center;
			border-radius: 50%;
			border: 1px solid var(--color-border);
			color: var(--color-complement-text);
			font-size: var(--font-s);
			cursor: pointer;
			transition: background-color 0.15s, border-color 0.15s;

			&:hover {
				border-color: var(--color-highlight);
			}
		}
	}

	&-actions {
		display: flex;
		justify-content: flex-end;
		gap: 6px;
		margin-bottom: 4px;

		button {
			padding: 4px 10px;
			border-radius: 5px;
			font-size: var(--font-s);
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
			background-color: var(--color-highlight);
			border: none;
			color: white;
		}
	}

	&-legend {
		border-top: 1px solid var(--color-border);
		padding-top: 8px;
		display: flex;
		flex-direction: column;
		gap: 4px;

		&-item {
			display: flex;
			align-items: center;
			gap: 7px;
			font-size: var(--font-s);
			color: var(--color-complement-text);
		}

		&-swatch {
			width: 10px;
			height: 10px;
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
