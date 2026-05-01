<!-- 通勤圈（Isochrone）分析對話框 -->

<script setup>
import { ref } from "vue";
import { useMapStore } from "../../store/mapStore";
import { useDialogStore } from "../../store/dialogStore";

import DialogContainer from "./DialogContainer.vue";

const mapStore = useMapStore();
const dialogStore = useDialogStore();

const PRESET_ORIGINS = [
	{ label: "臺北車站", lng: 121.517, lat: 25.0478 },
	{ label: "板橋車站", lng: 121.4637, lat: 25.0144 },
	{ label: "臺北市政府", lng: 121.5654, lat: 25.0376 },
	{ label: "新北市政府", lng: 121.4636, lat: 25.0123 },
	{ label: "南港車站", lng: 121.6074, lat: 25.053 },
];

const PROFILES = [
	{ value: "driving-traffic", label: "開車（含路況）" },
	{ value: "driving", label: "開車" },
	{ value: "cycling", label: "腳踏車" },
	{ value: "walking", label: "步行" },
];

const CONTOUR_COLORS = ["2ecc71", "f1c40f", "e67e22", "e74c3c"];

const selectedOrigin = ref(PRESET_ORIGINS[0]);
const selectedProfile = ref("driving-traffic");
const selectedMinutes = ref([15, 30, 45, 60]);

function toggleMinute(m) {
	if (selectedMinutes.value.includes(m)) {
		if (selectedMinutes.value.length > 1)
			selectedMinutes.value = selectedMinutes.value.filter((x) => x !== m);
	} else {
		selectedMinutes.value = [...selectedMinutes.value, m].sort(
			(a, b) => a - b,
		);
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

function handleClose() {
	dialogStore.hideAllDialogs();
}
</script>

<template>
  <DialogContainer
    dialog="isochronePanel"
    @on-close="handleClose"
  >
    <div class="isochrone">
      <h2>通勤圈分析</h2>

      <!-- 起點選擇 -->
      <div class="isochrone-section">
        <label>起點</label>
        <select v-model="selectedOrigin">
          <option
            v-for="o in PRESET_ORIGINS"
            :key="o.label"
            :value="o"
          >
            {{ o.label }}
          </option>
        </select>
      </div>

      <!-- 交通模式 -->
      <div class="isochrone-section">
        <label>交通模式</label>
        <div class="isochrone-profiles">
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

      <!-- 時間選擇 -->
      <div class="isochrone-section">
        <label>等時圈（分鐘）</label>
        <div class="isochrone-chips">
          <span
            v-for="(m, i) in [15, 30, 45, 60]"
            :key="m"
            :class="{ active: selectedMinutes.includes(m) }"
            :style="selectedMinutes.includes(m) ? { background: '#' + CONTOUR_COLORS[i], color: '#000' } : {}"
            @click="toggleMinute(m)"
          >{{ m }}</span>
        </div>
      </div>

      <!-- 操作按鈕 -->
      <div class="isochrone-control">
        <button
          class="isochrone-control-clear"
          @click="clear"
        >
          清除
        </button>
        <button
          class="isochrone-control-calc"
          :disabled="mapStore.isochroneState.isLoading"
          @click="calculate"
        >
          {{ mapStore.isochroneState.isLoading ? "計算中..." : "計算通勤圈" }}
        </button>
      </div>

      <!-- 圖例 -->
      <div
        v-if="mapStore.isochroneState.visible"
        class="isochrone-legend"
      >
        <div
          v-for="(m, i) in selectedMinutes"
          :key="m"
          class="isochrone-legend-item"
        >
          <span
            class="isochrone-legend-swatch"
            :style="{ background: '#' + CONTOUR_COLORS[i] }"
          />
          {{ m }} 分鐘以內
        </div>
      </div>
    </div>
  </DialogContainer>
</template>

<style scoped lang="scss">
.isochrone {
	width: 280px;

	h2 {
		margin-bottom: 12px;
	}

	&-section {
		display: flex;
		flex-direction: column;
		margin-bottom: 12px;

		label {
			margin-bottom: 6px;
			font-size: var(--font-s);
			color: var(--color-complement-text);
		}

		select {
			padding: 4px 6px;
			border-radius: 5px;
			border: 1px solid var(--color-border);
			background-color: var(--color-component-background);
			color: var(--color-normal-text);
			font-size: var(--font-s);
			cursor: pointer;
		}
	}

	&-profiles {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;

		button {
			padding: 3px 8px;
			border-radius: 5px;
			border: 1px solid var(--color-border);
			background-color: transparent;
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
		gap: 8px;

		span {
			width: 2.2rem;
			height: 2.2rem;
			display: flex;
			align-items: center;
			justify-content: center;
			border-radius: 50%;
			border: 1px solid var(--color-border);
			color: var(--color-complement-text);
			font-size: var(--font-s);
			cursor: pointer;
			transition: background-color 0.2s, color 0.2s, border-color 0.2s;

			&:hover {
				border-color: var(--color-highlight);
			}
		}
	}

	&-control {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
		margin-top: 4px;

		button {
			padding: 4px 10px;
			border-radius: 5px;
			font-size: var(--font-s);
			cursor: pointer;
			transition: opacity 0.2s;

			&:hover {
				opacity: 0.8;
			}

			&:disabled {
				opacity: 0.5;
				cursor: not-allowed;
			}
		}

		&-clear {
			background-color: transparent;
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
		margin-top: 12px;
		padding-top: 10px;
		border-top: 1px solid var(--color-border);
		display: flex;
		flex-direction: column;
		gap: 5px;

		&-item {
			display: flex;
			align-items: center;
			gap: 8px;
			font-size: var(--font-s);
			color: var(--color-complement-text);
		}

		&-swatch {
			width: 12px;
			height: 12px;
			border-radius: 2px;
			flex-shrink: 0;
		}
	}
}
</style>
