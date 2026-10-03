    const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
    const $ = (id) => document.getElementById(id);
    const fields = {
      configPath: $("configPath"),
      loginBtn: $("loginBtn"),
      roomType: $("roomType"),
      floorId: $("floorId"),
      seatNum: $("seatNum"),
      fallbackSeats: $("fallbackSeats"),
      startHour: $("startHour"),
      durationHours: $("durationHours"),
      executeAt: $("executeAt"),
      holdBeforeMinutes: $("holdBeforeMinutes"),
      maxTrials: $("maxTrials"),
      retryDelay: $("retryDelay"),
      dryRun: $("dryRun"),
      bookingSelect: $("bookingSelect"),
      bookingMeta: $("bookingMeta"),
      seatBadge: $("seatBadge"),
      seatMetric: $("seatMetric"),
      floorMetric: $("floorMetric"),
      timeMetric: $("timeMetric"),
      submitMetric: $("submitMetric"),
      planSummary: $("planSummary"),
      railStart: $("railStart"),
      railSubmit: $("railSubmit"),
      clock: $("clock"),
      logBox: $("logBox"),
      notice: $("notice"),
      status: $("status"),
      loadBtn: $("loadBtn"),
      saveBtn: $("saveBtn"),
      instantRunBtn: $("instantRunBtn"),
      runBtn: $("runBtn"),
      cancelBtn: $("cancelBtn"),
      refreshBookingsBtn: $("refreshBookingsBtn"),
      measureClockBtn: $("measureClockBtn"),
      clockResult: $("clockResult"),
      cancelBookingBtn: $("cancelBookingBtn"),
      checkInTestBtn: $("checkInTestBtn"),
      autoCheckInBtn: $("autoCheckInBtn"),
      continueSeatBtn: $("continueSeatBtn"),
      clearBtn: $("clearBtn"),
      mapViewport: $("mapViewport"),
      mapMessage: $("mapMessage"),
      mapZoom: $("mapZoom"),
      refreshMapBtn: $("refreshMapBtn"),
      zoomOutBtn: $("zoomOutBtn"),
      zoomInBtn: $("zoomInBtn"),
      pickPrimaryBtn: $("pickPrimaryBtn"),
      pickFallbackBtn: $("pickFallbackBtn"),
    };
    const formControls = [
      fields.configPath,
      fields.roomType,
      fields.floorId,
      fields.seatNum,
      fields.fallbackSeats,
      fields.startHour,
      fields.durationHours,
      fields.executeAt,
      fields.holdBeforeMinutes,
      fields.maxTrials,
      fields.retryDelay,
      fields.dryRun,
      fields.bookingSelect,
      ...document.querySelectorAll("input[name='days']"),
    ];

    let pollTimer = null;
    let pollFailures = 0;
    let currentJobId = "";
    let currentBookings = [];
    let busyState = false;
    let clockMeasuring = false;
    let seatMapData = null;
    let mapRequestId = 0;
    let mapZoom = 1;
    let mapMode = "primary";

    function setMapMessage(message, error = false) {
      fields.mapMessage.textContent = message;
      fields.mapMessage.className = `map-message${error ? " error" : ""}`;
    }

    function fallbackNumbers() {
      return fields.fallbackSeats.value.split(/[,，\s]+/).map((value) => value.trim()).filter(Boolean);
    }

    function mapSeatStatus(state, exact) {
      if (!exact) return { cls: "unknown", label: "状态未查询" };
      if (state === "0") return { cls: "available", label: "可用" };
      if (state === "1") return { cls: "busy", label: "已占用" };
      if (state === "2" || state === "4") return { cls: "locked", label: "锁定" };
      if (state === "3") return { cls: "closed", label: "关闭" };
      return { cls: "unknown", label: "状态未知" };
    }

    function setMapMode(mode) {
      mapMode = mode;
      fields.pickPrimaryBtn.setAttribute("aria-pressed", String(mode === "primary"));
      fields.pickFallbackBtn.setAttribute("aria-pressed", String(mode === "fallback"));
    }

    function chooseMapSeat(number) {
      if (busyState) return;
      const currentPrimary = fields.seatNum.value.trim();
      const fallbacks = fallbackNumbers();
      const item = seatMapData && seatMapData.seats.find((seat) => seat.number === number);
      const status = mapSeatStatus(item && item.state, seatMapData && seatMapData.availability_exact);
      const warning = ["busy", "locked", "closed"].includes(status.cls)
        ? `；当前时段显示${status.label}，请留意状态变化` : "";
      if (mapMode === "primary") {
        fields.seatNum.value = number;
        fields.fallbackSeats.value = fallbacks.filter((item) => item !== number).join(",");
        setMapMessage(`已选 ${number} 座为主座位${warning}`);
      } else if (number === currentPrimary) {
        setMapMessage(`${number} 座已经是主座位`);
        return;
      } else if (fallbacks.includes(number)) {
        fields.fallbackSeats.value = fallbacks.filter((item) => item !== number).join(",");
        setMapMessage(`已移除备选 ${number} 座`);
      } else if (fallbacks.length >= 5) {
        setMapMessage("备选座位最多选择 5 个", true);
        return;
      } else {
        fields.fallbackSeats.value = [...fallbacks, number].join(",");
        setMapMessage(`已添加备选 ${number} 座${warning}`);
      }
      updatePlanSummary();
      renderSeatMap();
    }

    function renderSeatMap(centerSelection = false) {
      if (!seatMapData) return;
      const viewport = fields.mapViewport;
      const previousLeft = viewport.scrollLeft;
      const previousTop = viewport.scrollTop;
      const ns = "http://www.w3.org/2000/svg";
      const make = (tag) => document.createElementNS(ns, tag);
      const map = seatMapData;
      const svg = make("svg");
      const pixelsPerUnit = 12 * mapZoom;
      svg.setAttribute("viewBox", `0 0 ${map.width} ${map.height}`);
      svg.setAttribute("width", String(map.width * pixelsPerUnit));
      svg.setAttribute("height", String(map.height * pixelsPerUnit));
      svg.setAttribute("role", "img");
      svg.setAttribute("aria-label", `${map.floor_name}座位位置图，${map.seats.length}个座位`);
      if (map.plan_url) {
        const plan = make("image");
        plan.setAttribute("href", map.plan_url);
        plan.setAttribute("x", "0"); plan.setAttribute("y", "0");
        plan.setAttribute("width", String(map.width));
        plan.setAttribute("height", String(map.height));
        plan.setAttribute("preserveAspectRatio", "none");
        svg.appendChild(plan);
      }
      const primary = fields.seatNum.value.trim();
      const backups = new Set(fallbackNumbers());
      let selected = null;
      map.seats.forEach((seat) => {
        const group = make("g");
        const status = mapSeatStatus(seat.state, map.availability_exact);
        const role = seat.number === primary ? "primary" : backups.has(seat.number) ? "fallback" : status.cls;
        group.setAttribute("class", `map-seat ${role}`);
        group.setAttribute("tabindex", "0");
        group.setAttribute("role", "button");
        group.setAttribute("aria-label", `${seat.number} 座，${status.label}${role === "primary" ? "，主座位" : role === "fallback" ? "，备选座位" : ""}`);
        const rect = make("rect");
        rect.setAttribute("x", String(seat.x)); rect.setAttribute("y", String(seat.y));
        rect.setAttribute("width", String(Math.max(seat.w, 1.7)));
        rect.setAttribute("height", String(Math.max(seat.h, 1.7)));
        rect.setAttribute("rx", ".22");
        group.appendChild(rect);
        const label = make("text");
        label.setAttribute("x", String(seat.x + Math.max(seat.w, 1.7) / 2));
        label.setAttribute("y", String(seat.y + Math.max(seat.h, 1.7) / 2 + .36));
        label.setAttribute("text-anchor", "middle");
        label.textContent = seat.number;
        group.appendChild(label);
        group.addEventListener("click", () => chooseMapSeat(seat.number));
        group.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault(); chooseMapSeat(seat.number);
          }
        });
        svg.appendChild(group);
        if (seat.number === primary) selected = seat;
      });
      viewport.replaceChildren(svg);
      if (centerSelection && selected) {
        viewport.scrollLeft = Math.max(0, (selected.x + selected.w / 2) * pixelsPerUnit - viewport.clientWidth / 2);
        viewport.scrollTop = Math.max(0, (selected.y + selected.h / 2) * pixelsPerUnit - viewport.clientHeight / 2);
      } else {
        viewport.scrollLeft = previousLeft;
        viewport.scrollTop = previousTop;
      }
      fields.mapZoom.textContent = `${Math.round(mapZoom * 100)}%`;
    }

    async function loadSeatMap() {
      const requestId = ++mapRequestId;
      const floorId = fields.floorId.value.trim();
      const start = Number(fields.startHour.value);
      const duration = Number(fields.durationHours.value);
      if (!floorId || !fields.startHour.value || !fields.durationHours.value ||
          !Number.isInteger(start) || !Number.isInteger(duration) || start < 0 ||
          start > 23 || duration < 1 || start + duration > 24) {
        seatMapData = null;
        fields.mapViewport.innerHTML = '<div class="map-empty">请选择楼层和有效预约时段</div>';
        setMapMessage("");
        return;
      }
      fields.mapViewport.innerHTML = '<div class="map-empty">正在读取官方座位图…</div>';
      setMapMessage("");
      try {
        const params = new URLSearchParams({
          path: fields.configPath.value.trim(), room_type: fields.roomType.value.trim(),
          floor_id: floorId, days: String(selectedDays()),
          start_hour: String(start), duration_hours: String(duration),
        });
        const data = await requestJson(`/api/seat-map?${params}`, {}, 12000);
        if (requestId !== mapRequestId) return;
        seatMapData = data;
        mapZoom = 1;
        renderSeatMap(true);
        setMapMessage(data.availability_exact
          ? `${data.floor_name} · ${data.seats.length} 座 · ${selectedDayText()} ${hourText(start)}-${hourText(start + duration)} 的状态，${data.fetched_at} 更新。状态会变化，提交时以接口结果为准。`
          : `${data.floor_name} · ${data.seats.length} 座 · 目标时段尚无座位状态，当前位置来自其他可查询时段；点击可选座。`);
      } catch (error) {
        if (requestId !== mapRequestId) return;
        seatMapData = null;
        fields.mapViewport.innerHTML = '<div class="map-empty">座位图暂时无法显示，可继续手动填写座位号</div>';
        setMapMessage(`读取座位图失败：${error.message}`, true);
      }
    }

    function numberValue(field, fallback = 0) {
      const parsed = Number(field.value);
      return Number.isFinite(parsed) ? parsed : fallback;
    }

    function hourText(hour) {
      if (!Number.isFinite(hour)) return "--:--";
      const normalized = Math.max(0, Math.min(24, Math.round(hour)));
      return `${String(normalized).padStart(2, "0")}:00`;
    }

    function selectedDayText() {
      const days = selectedDays();
      if (days === 0) return "今天";
      if (days === 1) return "明天";
      return "后天";
    }

    function selectedDays() {
      const item = document.querySelector("input[name='days']:checked");
      return item ? Number(item.value) : 1;
    }

    function setDays(days) {
      const item = document.querySelector(`input[name='days'][value="${days}"]`);
      (item || document.querySelector("input[name='days'][value='1']")).checked = true;
      updatePlanSummary();
    }

    function updateClock() {
      const now = new Date();
      fields.clock.textContent = now.toLocaleTimeString("zh-CN", { hour12: false });
    }

    function updatePlanSummary() {
      const seat = fields.seatNum.value.trim() || "--";
      const fallback = fields.fallbackSeats.value.trim();
      const floor = fields.floorId.value
        ? fields.floorId.selectedOptions[0].textContent : "--";
      const start = numberValue(fields.startHour, NaN);
      const duration = numberValue(fields.durationHours, NaN);
      const end = Number.isFinite(start) && Number.isFinite(duration) ? start + duration : NaN;
      const executeAt = fields.executeAt.value.trim() || "立即";
      const holdMinutes = Number(fields.holdBeforeMinutes.value) || 0;

      fields.seatBadge.textContent = seat;
      fields.seatMetric.textContent = seat;
      fields.floorMetric.textContent = floor === "--" ? "楼层 --" : floor;
      fields.timeMetric.textContent = `${hourText(start)}-${hourText(end)}`;
      fields.submitMetric.textContent = `${executeAt} 提交`;
      const fallbackText = fallback ? ` · 备选 ${fallback}` : "";
      const holdText = holdMinutes > 0 && executeAt !== "立即" ? ` · 提前 ${holdMinutes} 分钟预留` : "";
      fields.planSummary.textContent = `${selectedDayText()} · ${floor} / ${seat} 座${fallbackText} · ${hourText(start)}-${hourText(end)} · ${executeAt} 提交${holdText}`;
      fields.railStart.textContent = hourText(start);
      fields.railSubmit.textContent = executeAt;
    }

    function updateBookingMeta(item) {
      if (!item) {
        fields.bookingMeta.textContent = currentBookings.length ? "请选择一条预约" : "刷新当前预约后显示签到窗口和状态";
        return;
      }
      const windowText = item.sign_start_text && item.sign_deadline_text
        ? `签到 ${item.sign_start_text} 至 ${item.sign_deadline_text}`
        : "签到窗口暂未返回";
      const autoText = String(item.status) === "0" && item.auto_check_in_text
        ? ` · 自动签到 ${item.auto_check_in_text}`
        : "";
      const continueText = String(item.status) === "2"
        ? " · 可续座"
        : String(item.status) === "6" ? " · 续座期限已过" : "";
      fields.bookingMeta.textContent = `${item.status_label || "未知状态"}${continueText} · ${windowText}${autoText} · bookingId=${item.id}`;
    }

    function payload() {
      return {
        config_path: fields.configPath.value.trim(),
        room_type: fields.roomType.value.trim(),
        floor_id: fields.floorId.value.trim(),
        seat_num: fields.seatNum.value.trim(),
        fallback_seats: fields.fallbackSeats.value.trim(),
        start_hour: fields.startHour.value.trim(),
        duration_hours: fields.durationHours.value.trim(),
        execute_at: fields.executeAt.value.trim(),
        hold_before_minutes: fields.holdBeforeMinutes.value.trim(),
        max_trials: fields.maxTrials.value.trim(),
        retry_delay: fields.retryDelay.value.trim(),
        days: selectedDays(),
        dry_run: fields.dryRun.checked,
      };
    }

    function setStatus(text, cls = "") {
      fields.status.textContent = text;
      fields.status.className = `status ${cls}`;
    }

    function setNotice(text, cls = "") {
      fields.notice.textContent = text;
      fields.notice.className = `notice ${cls}`;
    }

    function appendLog(lines) {
      if (!Array.isArray(lines)) lines = [String(lines)];
      if (!lines.length) return;
      fields.logBox.textContent += lines.join("\n") + "\n";
      fields.logBox.scrollTop = fields.logBox.scrollHeight;
    }

    function setBusy(busy) {
      busyState = busy;
      formControls.forEach((control) => { control.disabled = busy; });
      fields.loginBtn.disabled = busy;
      fields.loadBtn.disabled = busy;
      fields.saveBtn.disabled = busy;
      fields.instantRunBtn.disabled = busy;
      fields.runBtn.disabled = busy;
      fields.cancelBtn.disabled = !busy;
      fields.refreshBookingsBtn.disabled = busy;
      fields.refreshMapBtn.disabled = busy;
      fields.pickPrimaryBtn.disabled = busy;
      fields.pickFallbackBtn.disabled = busy;
      fields.measureClockBtn.disabled = clockMeasuring;
      updateCancelBookingButton();
      setStatus(busy ? "执行中" : "就绪", busy ? "running" : "");
    }

    function selectedBooking() {
      return currentBookings.find((item) => String(item.id) === fields.bookingSelect.value);
    }

    function updateCancelBookingButton() {
      const item = selectedBooking();
      fields.cancelBookingBtn.disabled = busyState || !item || !item.cancelable;
      fields.checkInTestBtn.disabled = busyState || !item || String(item.status) !== "0";
      fields.autoCheckInBtn.disabled = busyState || !item || String(item.status) !== "0";
      fields.continueSeatBtn.disabled = busyState || !item || !item.continuable;
      fields.continueSeatBtn.title = item && String(item.status) === "6"
        ? "该预约已暂离未归结束，续座期限已过"
        : "仅暂离中的预约可以续座";
      updateBookingMeta(item);
    }

    function renderBookings(items) {
      const selectedId = fields.bookingSelect.value;
      currentBookings = Array.isArray(items) ? items : [];
      fields.bookingSelect.innerHTML = "";
      if (!currentBookings.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "暂无预约";
        fields.bookingSelect.appendChild(option);
        updateCancelBookingButton();
        return;
      }
      currentBookings.forEach((item) => {
        const option = document.createElement("option");
        option.value = item.id;
        option.textContent = item.label;
        fields.bookingSelect.appendChild(option);
      });
      if (currentBookings.some((item) => String(item.id) === selectedId)) {
        fields.bookingSelect.value = selectedId;
      } else {
        const firstCancelable = currentBookings.find((item) => item.cancelable);
        fields.bookingSelect.value = String((firstCancelable || currentBookings[0]).id);
      }
      updateCancelBookingButton();
    }

    async function requestJson(url, options = {}, timeoutMs = 0) {
      const controller = timeoutMs > 0 ? new AbortController() : null;
      const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
      try {
        const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
        if (!["GET", "HEAD"].includes((options.method || "GET").toUpperCase())) {
          headers["X-CSRF-Token"] = csrfToken;
        }
        const response = await fetch(url, {
          ...options,
          headers,
          ...(controller ? { signal: controller.signal } : {}),
        });
        const data = await response.json();
        if (!response.ok) {
          const error = new Error(data.error || "请求失败");
          error.status = response.status;
          error.code = data.code;
          throw error;
        }
        return data;
      } finally {
        if (timer !== null) clearTimeout(timer);
      }
    }

    async function loadConfig() {
      try {
        setNotice("");
        const path = encodeURIComponent(fields.configPath.value.trim());
        const data = await requestJson(`/api/config?path=${path}`);
        fields.configPath.value = data.config_path;
        fields.roomType.value = data.room_type;
        fields.floorId.value = data.floor_id;
        if (String(data.floor_id) !== fields.floorId.value) {
          fields.floorId.value = "";
        }
        fields.seatNum.value = data.seat_num;
        fields.fallbackSeats.value = data.fallback_seats || "";
        fields.startHour.value = data.start_hour;
        fields.durationHours.value = data.duration_hours;
        fields.executeAt.value = data.execute_at || "";
        fields.holdBeforeMinutes.value = data.hold_before_minutes || 0;
        fields.maxTrials.value = data.max_trials;
        fields.retryDelay.value = data.retry_delay;
        fields.dryRun.checked = Boolean(data.dry_run);
        setDays(data.days);
        updatePlanSummary();
        loadSeatMap();
        setNotice(fields.floorId.value ? "配置已载入" : `配置中的楼层 ID ${data.floor_id} 不在列表中，请重新选择楼层`, fields.floorId.value ? "ok" : "error");
        loadBookings({ quiet: true }).catch((error) => {
          if (fields.floorId.value) {
            setNotice(`配置已载入；预约列表刷新失败：${error.message}`, "error");
          }
        });
      } catch (error) {
        setNotice(error.message, "error");
      }
    }

    async function loadBookings(options = {}) {
      const quiet = Boolean(options.quiet);
      try {
        if (!quiet) setNotice("");
        const path = encodeURIComponent(fields.configPath.value.trim());
        const data = await requestJson(`/api/bookings?path=${path}`);
        renderBookings(data.items);
        if (!quiet) setNotice(`已刷新 ${data.items.length} 条预约`, "ok");
        return data;
      } catch (error) {
        if (!quiet) setNotice(error.message, "error");
        throw error;
      }
    }

    async function measureClock() {
      clockMeasuring = true;
      fields.measureClockBtn.disabled = true;
      fields.clockResult.hidden = false;
      fields.clockResult.className = "clock-result";
      fields.clockResult.textContent = "正在实时测量，约需几秒…";
      try {
        const path = encodeURIComponent(fields.configPath.value.trim());
        const data = await requestJson(`/api/clock-offset?path=${path}`);
        const recommendation = data.recommendation || {};
        const target = recommendation.execute_at
          ? `建议执行时间：${recommendation.execute_at}` : "暂不建议设置执行时间";
        const current = fields.executeAt.value.trim();
        const currentText = current ? `\n当前填写：${current}` : "";
        fields.clockResult.textContent = `测量于 ${data.measured_at || "刚刚"}\n${data.message}\n${target}${currentText}\n${recommendation.basis || ""}`
          + (busyState ? "\n已启动的任务仍按原执行时间运行。" : "");
        fields.clockResult.className = recommendation.execute_at ? "clock-result" : "clock-result error";
      } catch (error) {
        fields.clockResult.className = "clock-result error";
        fields.clockResult.textContent = `测量失败：${error.message}`;
      } finally {
        clockMeasuring = false;
        fields.measureClockBtn.disabled = false;
      }
    }

    async function savePlan() {
      if (!fields.floorId.value) {
        setNotice("请先选择楼层", "error");
        fields.floorId.focus();
        return;
      }
      try {
        setNotice("");
        const data = await requestJson("/api/save", {
          method: "POST",
          body: JSON.stringify(payload()),
        });
        setNotice(data.message, "ok");
      } catch (error) {
        setNotice(error.message, "error");
      }
    }

    function bookingTargetText() {
      const start = numberValue(fields.startHour, NaN);
      const duration = numberValue(fields.durationHours, NaN);
      const end = Number.isFinite(start) && Number.isFinite(duration) ? start + duration : NaN;
      const fallback = fields.fallbackSeats.value.trim();
      const fallbackText = fallback ? ` · 备选 ${fallback}` : "";
      const floor = fields.floorId.value
        ? fields.floorId.selectedOptions[0].textContent : "--";
      return `${selectedDayText()} · ${floor} · `
        + `${fields.seatNum.value.trim() || "--"} 座${fallbackText} · ${hourText(start)}-${hourText(end)}`;
    }

    async function startBooking(immediate = false) {
      const requestPayload = payload();
      if (!requestPayload.floor_id) {
        setNotice("请先选择楼层", "error");
        fields.floorId.focus();
        return;
      }
      if (!immediate && !requestPayload.execute_at) {
        setNotice("定时预约需要填写执行时间；如需马上提交，请点击“立即预约”", "error");
        return;
      }
      if (!fields.dryRun.checked) {
        const holdMinutes = Number(requestPayload.hold_before_minutes) || 0;
        const prompt = immediate
          ? `确认立即预约？\n${bookingTargetText()}\n确认后将马上向预约接口提交。`
          : `确认定时预约？\n${bookingTargetText()}\n将在 ${requestPayload.execute_at} 提交。`
            + (holdMinutes > 0 ? `\n提前 ${holdMinutes} 分钟尝试临时预留主座位；预留成功仍须到点提交正式预约。` : "");
        if (!confirm(prompt)) return;
      }
      try {
        setNotice("");
        fields.logBox.textContent = "";
        setBusy(true);
        appendLog(immediate ? "开始立即预约流程..." : "开始定时预约流程...");
        const data = await requestJson(immediate ? "/api/run-now" : "/api/run", {
          method: "POST",
          body: JSON.stringify(requestPayload),
        });
        currentJobId = data.job_id;
        pollFailures = 0;
        pollJob(data.job_id);
      } catch (error) {
        setBusy(false);
        setStatus("失败", "error");
        setNotice(error.message, "error");
      }
    }

    async function runBooking() {
      return startBooking(false);
    }

    async function runBookingNow() {
      return startBooking(true);
    }

    async function cancelJob() {
      if (!currentJobId) return;
      try {
        const data = await requestJson("/api/cancel", {
          method: "POST",
          body: JSON.stringify({ job_id: currentJobId }),
        });
        setNotice(data.message, "ok");
      } catch (error) {
        setNotice(error.message, "error");
      }
    }

    async function cancelSelectedBooking() {
      const item = selectedBooking();
      if (!item) return;
      if (!item.cancelable) {
        setNotice("当前预约状态不可取消", "error");
        return;
      }
      const ok = confirm(`确认取消 ${item.label}？`);
      if (!ok) return;
      try {
        setNotice("");
        const data = await requestJson("/api/cancel-booking", {
          method: "POST",
          body: JSON.stringify({
            config_path: fields.configPath.value.trim(),
            booking_id: item.id,
          }),
        });
        setNotice(data.message, "ok");
        await loadBookings({ quiet: true });
      } catch (error) {
        setNotice(error.message, "error");
      }
    }

    async function checkInSelectedBooking() {
      const item = selectedBooking();
      if (!item) return;
      if (String(item.status) !== "0") {
        setNotice("只允许测试待签到预约", "error");
        return;
      }
      const ok = confirm(
        `确认发送签到接口测试？\n\n当前预约：${item.label}\n\n`
        + "这会真实请求 checkIn 接口；若后端放行，预约状态会变为签到成功。"
      );
      if (!ok) return;
      try {
        setNotice("");
        fields.checkInTestBtn.disabled = true;
        appendLog(`发送签到接口测试：bookingId=${item.id}`);
        const data = await requestJson("/api/check-in-test", {
          method: "POST",
          body: JSON.stringify({
            config_path: fields.configPath.value.trim(),
            booking_id: item.id,
          }),
        });
        appendLog(JSON.stringify(data, null, 2));
        if (data.sent) {
          const result = data.response && data.response.DATA && data.response.DATA.result;
          const msg = data.response && data.response.DATA && (data.response.DATA.msg || data.response.DATA.message);
          setNotice(msg || (result === "success" ? "签到接口返回成功" : "签到接口已返回"), result === "success" ? "ok" : "error");
        } else {
          setNotice(data.message || "预检未通过，未发送签到请求", "error");
        }
        await loadBookings({ quiet: true });
      } catch (error) {
        setNotice(error.message, "error");
      } finally {
        updateCancelBookingButton();
      }
    }

    async function autoCheckInSelectedBooking() {
      const item = selectedBooking();
      if (!item) return;
      if (String(item.status) !== "0") {
        setNotice("只允许对待签到预约启用自动签到", "error");
        return;
      }
      const autoTime = item.auto_check_in_text || "预约开始 5 分钟后";
      const ok = confirm(
        `确认自动签到？\n\n当前预约：${item.label}\n\n`
        + `系统会等到 ${autoTime} 后真实请求 checkIn 接口。`
      );
      if (!ok) return;
      try {
        setNotice("");
        fields.logBox.textContent = "";
        setBusy(true);
        appendLog(`开始自动签到任务：bookingId=${item.id}`);
        const data = await requestJson("/api/auto-check-in", {
          method: "POST",
          body: JSON.stringify({
            config_path: fields.configPath.value.trim(),
            booking_id: item.id,
            delay_minutes: 5,
          }),
        });
        currentJobId = data.job_id;
        pollFailures = 0;
        pollJob(data.job_id);
      } catch (error) {
        setBusy(false);
        setStatus("失败", "error");
        setNotice(error.message, "error");
      }
    }

    async function continueSelectedSeat() {
      const item = selectedBooking();
      if (!item) return;
      if (!item.continuable) {
        const message = String(item.status) === "6"
          ? "该预约已暂离未归结束，续座期限已过，服务器不允许续座"
          : `当前状态为${item.status_label || "未知"}，只有“暂离中”的预约可以续座`;
        setNotice(message, "error");
        return;
      }
      const ok = confirm(
        `确认续座？\n\n当前预约：${item.label}\n\n`
        + "这会真实请求服务器的 comeBack 接口，并在完成后复核预约状态。"
      );
      if (!ok) return;
      try {
        setNotice("");
        fields.continueSeatBtn.disabled = true;
        appendLog(`发送续座请求：bookingId=${item.id}`);
        const data = await requestJson("/api/continue-seat", {
          method: "POST",
          body: JSON.stringify({
            config_path: fields.configPath.value.trim(),
            booking_id: item.id,
          }),
        });
        appendLog(JSON.stringify(data, null, 2));
        setNotice(data.message || data.status_label_after || "续座成功", "ok");
        await loadBookings({ quiet: true });
      } catch (error) {
        setNotice(error.message, "error");
      } finally {
        updateCancelBookingButton();
      }
    }

    async function loginWithBrowser() {
      try {
        setBusy(true);
        fields.logBox.textContent = "";
        setNotice("请在新打开的浏览器中完成登录，Cookie 会自动保存", "ok");
        const data = await requestJson("/api/login", {
          method: "POST",
          body: JSON.stringify({config_path: fields.configPath.value.trim()}),
        });
        currentJobId = data.job_id;
        pollFailures = 0;
        pollJob(currentJobId);
      } catch (error) {
        setBusy(false);
        setStatus("失败", "error");
        setNotice(error.message, "error");
      }
    }

    async function pollJob(jobId) {
      if (jobId !== currentJobId) return;
      clearTimeout(pollTimer);
      pollTimer = null;
      try {
        const data = await requestJson(`/api/job?id=${encodeURIComponent(jobId)}`, {}, 5000);
        if (jobId !== currentJobId) return;
        if (pollFailures > 0) {
          setStatus("执行中", "running");
          setNotice("连接已恢复，已接回任务状态", "ok");
        }
        pollFailures = 0;
        fields.logBox.textContent = data.logs.join("\n") + (data.logs.length ? "\n" : "");
        fields.logBox.scrollTop = fields.logBox.scrollHeight;
        if (data.status === "running") {
          pollTimer = setTimeout(() => pollJob(jobId), data.poll_after_ms || 1000);
          return;
        }
        setBusy(false);
        currentJobId = "";
        loadBookings({ quiet: true }).catch(() => {});
        if (data.status === "error") {
          setStatus("失败", "error");
          setNotice(data.error || "执行失败", "error");
        } else if (data.status === "uncertain") {
          setStatus("待确认", "error");
          setNotice(data.error || "操作结果待确认，请刷新预约列表", "error");
        } else if (data.status === "cancelled") {
          setStatus("已取消", "");
          setNotice("任务已取消", "ok");
        } else {
          setStatus("完成", "");
          setNotice(data.message || "执行完成", "ok");
        }
      } catch (error) {
        if (jobId !== currentJobId) return;
        if (error.code === "job_not_found") {
          setBusy(false);
          currentJobId = "";
          pollFailures = 0;
          setStatus("待确认", "error");
          setNotice("服务中已找不到此任务，无法确认是否提交；请刷新预约列表核实结果", "error");
          loadBookings({ quiet: true }).catch(() => {});
          return;
        }
        pollFailures = Math.min(pollFailures + 1, 5);
        const delay = Math.min(1000 * (2 ** (pollFailures - 1)), 10000);
        setBusy(true);
        setStatus("连接恢复中", "running");
        setNotice(`暂时无法读取任务状态，${delay / 1000} 秒后重连；后台任务可能仍在运行`, "error");
        pollTimer = setTimeout(() => pollJob(jobId), delay);
      }
    }

    async function resumeActiveJob() {
      const data = await requestJson("/api/active-job");
      if (!data.job) return;
      currentJobId = data.job.id;
      pollFailures = 0;
      fields.logBox.textContent = data.job.logs.join("\n") + (data.job.logs.length ? "\n" : "");
      fields.logBox.scrollTop = fields.logBox.scrollHeight;
      setBusy(true);
      setNotice("已接回正在运行的任务", "ok");
      pollJob(currentJobId);
    }

    fields.loginBtn.addEventListener("click", loginWithBrowser);
    fields.loadBtn.addEventListener("click", loadConfig);
    fields.saveBtn.addEventListener("click", savePlan);
    fields.instantRunBtn.addEventListener("click", runBookingNow);
    fields.runBtn.addEventListener("click", runBooking);
    fields.cancelBtn.addEventListener("click", cancelJob);
    fields.refreshBookingsBtn.addEventListener("click", () => loadBookings());
    fields.measureClockBtn.addEventListener("click", measureClock);
    fields.cancelBookingBtn.addEventListener("click", cancelSelectedBooking);
    fields.checkInTestBtn.addEventListener("click", checkInSelectedBooking);
    fields.autoCheckInBtn.addEventListener("click", autoCheckInSelectedBooking);
    fields.continueSeatBtn.addEventListener("click", continueSelectedSeat);
    fields.bookingSelect.addEventListener("change", updateCancelBookingButton);
    fields.clearBtn.addEventListener("click", () => { fields.logBox.textContent = ""; });
    fields.refreshMapBtn.addEventListener("click", loadSeatMap);
    fields.pickPrimaryBtn.addEventListener("click", () => setMapMode("primary"));
    fields.pickFallbackBtn.addEventListener("click", () => setMapMode("fallback"));
    fields.zoomOutBtn.addEventListener("click", () => {
      if (!seatMapData) return;
      mapZoom = Math.max(0.6, Math.round((mapZoom - 0.2) * 10) / 10);
      renderSeatMap();
    });
    fields.zoomInBtn.addEventListener("click", () => {
      if (!seatMapData) return;
      mapZoom = Math.min(2.4, Math.round((mapZoom + 0.2) * 10) / 10);
      renderSeatMap();
    });
    formControls.forEach((control) => {
      control.addEventListener("input", updatePlanSummary);
      control.addEventListener("change", updatePlanSummary);
    });
    [fields.floorId, fields.roomType, fields.startHour, fields.durationHours,
      fields.configPath, ...document.querySelectorAll("input[name='days']")]
      .forEach((control) => control.addEventListener("change", () => {
        if (!busyState) loadSeatMap();
      }));
    [fields.seatNum, fields.fallbackSeats].forEach((control) => {
      control.addEventListener("input", () => renderSeatMap());
    });
    document.querySelectorAll("input[type='number']").forEach((input) => {
      input.addEventListener("focus", () => input.select());
      input.addEventListener("wheel", (event) => event.preventDefault(), { passive: false });
    });

    fields.configPath.value = document.querySelector('meta[name="default-config"]').content;
    updateClock();
    setInterval(updateClock, 1000);
    setDays(0);
    loadConfig().then(() => resumeActiveJob().catch(() => {}));
