<script setup>
import { ref, reactive, computed, watch, onMounted } from "vue";
import http from "../../router/axios";
import SearchableSelect from "./SearchableSelect.vue";

const props = defineProps([
	"chart_config",
	"activeChart",
	"activeCity",
	"series",
]);

const cities = ref([]);
const maxTransfers = ref(0);
const resultLoading = ref(false);
const resultError = ref("");
const directRoutes = ref([]);
const transferRoutes = ref([]);
const expandedTransfers = reactive({});

const validDistricts = {
	taipei: [
		"中正區",
		"大同區",
		"中山區",
		"松山區",
		"大安區",
		"萬華區",
		"信義區",
		"士林區",
		"北投區",
		"內湖區",
		"南港區",
		"文山區",
	],
	newtaipei: [
		"板橋區",
		"三重區",
		"中和區",
		"永和區",
		"新莊區",
		"新店區",
		"樹林區",
		"鶯歌區",
		"三峽區",
		"淡水區",
		"汐止區",
		"瑞芳區",
		"土城區",
		"蘆洲區",
		"五股區",
		"泰山區",
		"林口區",
		"深坑區",
		"石碇區",
		"坪林區",
		"三芝區",
		"石門區",
		"八里區",
		"平溪區",
		"雙溪區",
		"貢寮區",
		"金山區",
		"萬里區",
		"烏來區",
	],
};

function createSelectionState() {
	return reactive({
		city: "",
		district: "",
		stopId: "",
		districts: [],
		stops: [],
		loading: false,
	});
}

const fromState = createSelectionState();
const toState = createSelectionState();

const canSearch = computed(
	() => fromState.stopId && toState.stopId && fromState.city && toState.city,
);

function resetStops(state) {
	state.stopId = "";
	state.stops = [];
}

function resetDistrictAndStops(state) {
	state.district = "";
	state.districts = [];
	resetStops(state);
}

async function fetchCities() {
	try {
		const res = await http.get("/bus/lookup/cities");
		cities.value = res.data.data ?? [];
	} catch {
		cities.value = [];
	}
}

async function fetchDistricts(state) {
	state.loading = true;
	try {
		const res = await http.get("/bus/lookup/districts", {
			params: { city: state.city },
		});
		let rawDistricts = res.data.data ?? [];
		if (validDistricts[state.city]) {
			rawDistricts = rawDistricts.filter((d) =>
				validDistricts[state.city].includes(d),
			);
		}
		state.districts = rawDistricts;
	} catch {
		state.districts = [];
	} finally {
		state.loading = false;
	}
}

async function fetchStops(state) {
	state.loading = true;
	try {
		const res = await http.get("/bus/lookup/stops-by-district", {
			params: {
				city: state.city,
				district: state.district,
			},
		});

		const rawStops = res.data.data ?? [];
		const seen = new Set();
		state.stops = rawStops.filter((s) => {
			if (seen.has(s.stop_name)) return false;
			seen.add(s.stop_name);
			return true;
		});
	} catch {
		state.stops = [];
	} finally {
		state.loading = false;
	}
}

function clearResults() {
	directRoutes.value = [];
	transferRoutes.value = [];
	resultError.value = "";
}

async function fetchTransfers() {
	if (!canSearch.value) {
		clearResults();
		return;
	}
	resultLoading.value = true;
	resultError.value = "";
	try {
		const res = await http.get("/bus/transfer", {
			params: {
				city: fromState.city,
				to_city: toState.city,
				from_stop: fromState.stopId,
				to_stop: toState.stopId,
				max_transfers: maxTransfers.value,
			},
		});
		directRoutes.value = res.data.data?.direct_routes ?? [];
		transferRoutes.value = res.data.data?.transfer_routes ?? [];
	} catch (err) {
		resultError.value = "查詢失敗，請稍後再試";
		clearResults();
	} finally {
		resultLoading.value = false;
	}
}

function routeColor(routeName) {
	if (!routeName) return "#4fc3f7";
	let hash = 0;
	for (let i = 0; i < routeName.length; i += 1) {
		hash = routeName.charCodeAt(i) + ((hash << 5) - hash);
	}
	const hue = Math.abs(hash) % 360;
	return `hsl(${hue}, 70%, 55%)`;
}

const transferGroups = computed(() => {
	const groupMap = new Map();
	for (const item of transferRoutes.value) {
		if (!item?.route_a || !item?.route_b) continue;
		const key = `${item.route_a}__${item.route_b}`;
		if (!groupMap.has(key)) {
			groupMap.set(key, {
				key,
				route_a: item.route_a,
				route_b: item.route_b,
				stops: [],
				seenStops: new Set(),
			});
		}
		const group = groupMap.get(key);
		const stop = item.transfer_stop;
		const stopKey = stop?.stop_location_id ?? stop?.stop_name;
		if (stopKey && !group.seenStops.has(stopKey)) {
			group.seenStops.add(stopKey);
			group.stops.push(stop);
		}
	}
	const groups = Array.from(groupMap.values());
	for (const group of groups) {
		delete group.seenStops;
	}
	return groups;
});

const resultCombos = computed(() => {
	const combos = [];
	if (maxTransfers.value === 0) {
		for (const route of directRoutes.value.slice(0, 6)) {
			if (route?.route_name) {
				combos.push({
					key: `direct-${route.route_name}`,
					text: `搭${route.route_name}可直達`,
					type: "direct",
					title: "",
				});
			}
		}
	} else if (maxTransfers.value === 1) {
		for (const group of transferGroups.value.slice(0, 6)) {
			const stopNames = group.stops.map(s => s.stop_name).join("、");
			combos.push({
				key: `transfer-${group.key}`,
				text: `搭${group.route_a}轉${group.route_b}可抵達`,
				type: "transfer",
				title: `可轉乘站：${stopNames}`,
			});
		}
	}
	return combos;
});

const fromStopName = computed(() => {
	const s = fromState.stops.find(
		(s) => s.stop_location_id === fromState.stopId,
	);
	return s?.stop_name ?? fromState.stopId;
});

const toStopName = computed(() => {
	const s = toState.stops.find((s) => s.stop_location_id === toState.stopId);
	return s?.stop_name ?? toState.stopId;
});

function resolveDefaultCity() {
	if (props.activeCity === "newtaipei") return "newtaipei";
	if (props.activeCity === "taipei" || props.activeCity === "metrotaipei") {
		return "taipei";
	}
	return "";
}

function applyDefaultCity() {
	const defaultCity = resolveDefaultCity();
	if (!defaultCity) return;
	if (!fromState.city) fromState.city = defaultCity;
	if (!toState.city) toState.city = defaultCity;
}

async function fetchTopDirectStops(city) {
	try {
		const res = await http.get("/bus/lookup/top-direct-stops", {
			params: { city },
		});
		return res.data.data ?? null;
	} catch {
		return null;
	}
}

async function applyTopDirectStops() {
	const defaultCity = resolveDefaultCity();
	const fallbackCity = cities.value?.[0]?.value;
	const city = defaultCity || fallbackCity;
	if (!city) return;

	const topPair = await fetchTopDirectStops(city);
	if (!topPair?.from_stop_location_id || !topPair?.to_stop_location_id) {
		applyDefaultCity();
		return;
	}

	fromState.city = city;
	toState.city = city;

	await Promise.all([fetchDistricts(fromState), fetchDistricts(toState)]);

	fromState.district = topPair.from_district || "";
	toState.district = topPair.to_district || "";

	await Promise.all([fetchStops(fromState), fetchStops(toState)]);

	fromState.stopId = topPair.from_stop_location_id;
	toState.stopId = topPair.to_stop_location_id;
}

watch(
	() => fromState.city,
	async (next) => {
		resetDistrictAndStops(fromState);
		if (next) await fetchDistricts(fromState);
	},
);

watch(
	() => toState.city,
	async (next) => {
		resetDistrictAndStops(toState);
		if (next) await fetchDistricts(toState);
	},
);

watch(
	() => fromState.district,
	async (next) => {
		resetStops(fromState);
		if (next && fromState.city) await fetchStops(fromState);
	},
);

watch(
	() => toState.district,
	async (next) => {
		resetStops(toState);
		if (next && toState.city) await fetchStops(toState);
	},
);

watch(
	() => [fromState.stopId, toState.stopId, maxTransfers.value],
	() => {
		fetchTransfers();
	},
);

onMounted(() => {
	fetchCities().finally(() => {
		applyTopDirectStops();
	});
});

watch(
	() => props.activeCity,
	() => {
		applyTopDirectStops();
	},
);

function toggleTransferGroup(key) {
	expandedTransfers[key] = !expandedTransfers[key];
}

function activateTransferGroup(comboKey) {
	const key = comboKey.replace(/^transfer-/, "");
	maxTransfers.value = 1;
	expandedTransfers[key] = true;
}
</script>

<template>
	<div class="bus-transfer-chart">
		<div class="btc-selectors">
			<div class="btc-card">
				<div class="btc-card__title">起點</div>
				<div class="btc-fields-grid">
					<div class="btc-field">
						<label>城市</label>
						<SearchableSelect
							v-model="fromState.city"
							:options="cities"
						/>
					</div>
					<div class="btc-field">
						<label>行政區</label>
						<SearchableSelect
							v-model="fromState.district"
							:options="
								fromState.districts.map((d) => ({
									label: d,
									value: d,
								}))
							"
							:disabled="!fromState.city"
						/>
					</div>
					<div class="btc-field">
						<label>公車站</label>
						<SearchableSelect
							v-model="fromState.stopId"
							:options="
								fromState.stops.map((s) => ({
									label: s.stop_name,
									value: s.stop_location_id,
								}))
							"
							:disabled="!fromState.district"
						/>
					</div>
				</div>
			</div>

			<div class="btc-card">
				<div class="btc-card__title">終點</div>
				<div class="btc-fields-grid">
					<div class="btc-field">
						<label>城市</label>
						<SearchableSelect
							v-model="toState.city"
							:options="cities"
						/>
					</div>
					<div class="btc-field">
						<label>行政區</label>
						<SearchableSelect
							v-model="toState.district"
							:options="
								toState.districts.map((d) => ({
									label: d,
									value: d,
								}))
							"
							:disabled="!toState.city"
						/>
					</div>
					<div class="btc-field">
						<label>公車站</label>
						<SearchableSelect
							v-model="toState.stopId"
							:options="
								toState.stops.map((s) => ({
									label: s.stop_name,
									value: s.stop_location_id,
								}))
							"
							:disabled="!toState.district"
						/>
					</div>
				</div>
			</div>
		</div>

		<div class="btc-actions">
			<div class="btc-toggle">
				<button
					:class="{ active: maxTransfers === 0 }"
					@click="maxTransfers = 0"
				>
					直達
				</button>
				<button
					:class="{ active: maxTransfers === 1 }"
					@click="maxTransfers = 1"
				>
					一次轉乘
				</button>
			</div>
			<div v-if="false" class="btc-warning">起訖站需同一城市</div>
		</div>

		<div class="btc-results">
			<div v-if="resultLoading" class="btc-loading">
				<span class="material-icons">sync</span>
				查詢中...
			</div>

			<div v-else-if="!canSearch" class="btc-empty">請先選擇起訖站</div>

			<div v-else-if="resultError" class="btc-error">
				{{ resultError }}
			</div>

			<div v-else class="btc-result-content">
				<div class="btc-section">
					<h4>公車組合</h4>
					<div class="btc-combo-list">
						<div
							v-for="combo in resultCombos"
							:key="combo.key"
							class="btc-combo-tag"
							:class="`btc-combo-tag--${combo.type}`"
							:title="combo.title"
							@click="
								combo.type === 'transfer' &&
								activateTransferGroup(combo.key)
							"
						>
							{{ combo.text }}
						</div>
						<div v-if="!resultCombos.length" class="btc-empty">
							目前沒有可行組合
						</div>
					</div>
				</div>


				<div v-if="maxTransfers === 1" class="btc-section">
					<h4>一次轉乘</h4>
					<div class="btc-transfer-list">
						<div
							v-for="group in transferGroups.slice(0, 6)"
							:key="group.key"
							class="btc-transfer-group"
						>
							<button
								class="btc-transfer-summary"
								@click="toggleTransferGroup(group.key)"
							>
								<span
									>搭{{ group.route_a }}轉{{
										group.route_b
									}}可抵達</span
								>
								<span class="material-icons">
									{{
										expandedTransfers[group.key]
											? "expand_less"
											: "expand_more"
									}}
								</span>
							</button>
							<div
								v-if="expandedTransfers[group.key]"
								class="btc-transfer-details"
							>
								<div
									v-for="stop in group.stops"
									:key="
										stop.stop_location_id || stop.stop_name
									"
									class="btc-transfer-card"
								>
									<div class="btc-transfer-flow">
										<div class="btc-node">A</div>
										<div
											class="btc-line"
											:style="{
												'--route-color': routeColor(
													group.route_a,
												),
											}"
										/>
										<div
											class="btc-node btc-node--transfer"
										>
											T
										</div>
										<div
											class="btc-line"
											:style="{
												'--route-color': routeColor(
													group.route_b,
												),
											}"
										/>
										<div class="btc-node">B</div>
									</div>
									<div class="btc-transfer-info">
										<div
											class="btc-route-pill"
											:style="{
												'--route-color': routeColor(
													group.route_a,
												),
											}"
										>
											{{ group.route_a }}
										</div>
										<div class="btc-transfer-stop">
											轉乘站：{{ stop.stop_name }}
										</div>
										<div
											class="btc-route-pill"
											:style="{
												'--route-color': routeColor(
													group.route_b,
												),
											}"
										>
											{{ group.route_b }}
										</div>
									</div>
								</div>
							</div>
						</div>
						<div v-if="!transferGroups.length" class="btc-empty">
							目前沒有一次轉乘建議
						</div>
					</div>
				</div>
			</div>
		</div>
	</div>
</template>

<style scoped lang="scss">
.bus-transfer-chart {
	position: absolute;
	top: 0;
	left: 0;
	right: 0;
	bottom: 0;
	display: flex;
	overflow-y: auto;
	padding-right: 4px;
	padding-bottom: 80px;
	flex-direction: column;
	gap: 12px;
	box-sizing: border-box;
}

.bus-transfer-chart * {
}

.btc-selectors {
	display: grid;
	grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
	gap: 12px;
	flex-shrink: 0;
}

.btc-card {
	padding: 12px;
	border-radius: 10px;
	background: linear-gradient(
		135deg,
		rgba(40, 52, 71, 0.9),
		rgba(25, 33, 45, 0.9)
	);
	border: 1px solid rgba(71, 89, 110, 0.6);
	box-shadow: 0 8px 18px rgba(0, 0, 0, 0.25);
}

.btc-card__title {
	font-size: 14px;
	font-weight: 600;
	color: #e5edf7;
	margin-bottom: 8px;
}

.btc-fields-grid {
	display: flex;
	gap: 12px;
	justify-content: space-between;
}

.btc-field {
	display: flex;
	flex-direction: column;
	gap: 4px;
	flex: 1 1 0;
	min-width: 0;

	label {
		font-size: 11px;
		color: #9fb0c6;
	}

	:deep(.ss-display) {
		min-height: 30px;
		padding: 6px 10px;
	}

	:deep(.ss-value) {
		font-size: 12px;
	}
}

.btc-select {
	background: rgba(18, 26, 38, 0.9);
	border: 1px solid rgba(69, 90, 115, 0.8);
	border-radius: 8px;
	color: #d8e2f0;
	padding: 6px 8px;
	font-size: 13px;
	outline: none;
	transition: border-color 0.15s;
}

.btc-warning {
	font-size: 12px;
	color: #ffb74d;
}

.btc-actions {
	display: flex;
	align-items: center;
	justify-content: space-between;
	flex-shrink: 0;
}

.btc-toggle {
	display: flex;
	background: rgba(20, 28, 40, 0.9);
	border-radius: 999px;
	padding: 4px;
	gap: 4px;

	button {
		border: none;
		background: transparent;
		color: #9fb0c6;
		padding: 6px 12px;
		border-radius: 999px;
		font-size: 12px;
		cursor: pointer;
		transition: all 0.15s;

		&.active {
			background: linear-gradient(120deg, #2f80ed, #56ccf2);
			color: #0b1625;
			font-weight: 600;
		}
	}
}

.btc-results {
	flex: 1 1 auto;
	min-height: 320px;
	background: rgba(17, 24, 34, 0.6);
	border: 1px solid rgba(71, 89, 110, 0.3);
	border-radius: 12px;
	padding: 12px;
	overflow:visible;
}

.btc-loading {
	display: flex;
	align-items: center;
	gap: 8px;
	color: #b6c4d9;
	font-size: 13px;

	span {
		animation: spin 1s linear infinite;
	}
}

.btc-result-content {
	display: flex;
	flex-direction: column;
	gap: 16px;
}

.btc-section h4 {
	font-size: 14px;
	color: #e5edf7;
	margin-bottom: 8px;
}

.btc-route-list {
	display: grid;
	grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
	gap: 10px;
}

.btc-combo-list {
	display: flex;
	flex-wrap: wrap;
	gap: 8px;
	max-height: 150px;
	overflow-y: auto;
	padding-right: 4px;
}

.btc-combo-tag {
	padding: 4px 8px;
	border-radius: 999px;
	font-size: 11px;
	font-weight: 600;
}

.btc-combo-tag--direct {
	background: rgba(255, 88, 88, 0.12);
	border: 1px solid rgba(255, 88, 88, 0.45);
	color: #ff7a7a;
}

.btc-combo-tag--transfer {
	background: rgba(67, 208, 255, 0.15);
	border: 1px solid rgba(67, 208, 255, 0.5);
	color: #cfefff;
	cursor: pointer;
}

.btc-route-card {
	padding: 10px;
	border-radius: 10px;
	background: linear-gradient(
		140deg,
		rgba(20, 30, 45, 0.95),
		rgba(13, 20, 31, 0.95)
	);
	border: 1px solid color-mix(in srgb, var(--route-color) 60%, #0f1723 40%);
	box-shadow: 0 6px 14px rgba(0, 0, 0, 0.2);
}

.btc-route-badge {
	display: inline-block;
	font-size: 11px;
	padding: 2px 6px;
	border-radius: 999px;
	background: color-mix(in srgb, var(--route-color) 70%, #0f1723 30%);
	color: #0f1723;
	font-weight: 700;
}

.btc-route-name {
	display: block;
	margin-top: 6px;
	font-size: 14px;
	font-weight: 600;
	color: #dfe9f7;
}

.btc-transfer-list {
	display: flex;
	flex-direction: column;
	gap: 10px;
}

.btc-transfer-group {
	background: rgba(13, 20, 31, 0.8);
	border: 1px solid rgba(80, 98, 120, 0.5);
	border-radius: 12px;
	padding: 10px;
	display: flex;
	flex-direction: column;
	gap: 10px;
}

.btc-transfer-summary {
	border: none;
	background: rgba(20, 30, 45, 0.9);
	color: #e5edf7;
	border-radius: 10px;
	padding: 8px 10px;
	font-size: 12px;
	font-weight: 600;
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: 8px;
	cursor: pointer;
}

.btc-transfer-details {
	display: flex;
	flex-direction: column;
	gap: 10px;
}

.btc-transfer-card {
	background: rgba(16, 24, 36, 0.9);
	border: 1px solid rgba(80, 98, 120, 0.5);
	border-radius: 12px;
	padding: 12px;
	display: flex;
	flex-direction: column;
	gap: 10px;
}

.btc-transfer-flow {
	display: flex;
	align-items: center;
	gap: 8px;
}

.btc-node {
	width: 28px;
	height: 28px;
	border-radius: 50%;
	display: flex;
	align-items: center;
	justify-content: center;
	font-size: 12px;
	font-weight: 700;
	background: #1d2a3c;
	color: #d9e5f2;
	border: 1px solid rgba(94, 116, 145, 0.6);
}

.btc-node--transfer {
	background: #273249;
	color: #ffc857;
	border-color: rgba(255, 200, 87, 0.6);
}

.btc-line {
	flex: 1;
	height: 4px;
	border-radius: 999px;
	background: var(--route-color);
}

.btc-transfer-info {
	display: flex;
	align-items: center;
	flex-wrap: wrap;
	gap: 8px;
}

.btc-route-pill {
	padding: 4px 10px;
	border-radius: 999px;
	background: color-mix(in srgb, var(--route-color) 70%, #0f1723 30%);
	color: #0f1723;
	font-weight: 700;
	font-size: 12px;
}

.btc-transfer-stop {
	font-size: 12px;
	color: #cbd7e6;
}

.btc-empty,
.btc-error {
	font-size: 13px;
	color: #8fa4bd;
	text-align: center;
	padding: 12px 0;
}

@keyframes spin {
	to {
		transform: rotate(360deg);
	}
}

.btc-sql-block {
	flex-shrink: 0;
}

.btc-sql-toggle {
	display: flex;
	align-items: center;
	gap: 6px;
	background: transparent;
	border: 1px solid rgba(71, 89, 110, 0.5);
	border-radius: 8px;
	color: #7a9bb8;
	font-size: 12px;
	padding: 6px 12px;
	cursor: pointer;
	transition:
		color 0.15s,
		border-color 0.15s;

	&:hover {
		color: #b6d4ef;
		border-color: rgba(71, 89, 110, 0.9);
	}

	.material-icons {
		font-size: 16px;
	}
}

.btc-sql-pre {
	margin-top: 8px;
	padding: 12px;
	background: rgba(10, 16, 25, 0.85);
	border: 1px solid rgba(55, 72, 95, 0.6);
	border-radius: 8px;
	color: #7ecfff;
	font-size: 12px;
	font-family: "Fira Code", "Consolas", monospace;
	line-height: 1.6;
	white-space: pre;
	overflow-x: auto;
}
</style>
