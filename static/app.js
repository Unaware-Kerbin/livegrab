(function () {
  "use strict";

  var urlInput = document.getElementById("url");
  var form = document.getElementById("grab-form");
  var startBtn = document.getElementById("start");
  var destinationInput = document.getElementById("destination");
  var destinationNeeded = document.getElementById("destination-needed");
  var templateInput = document.getElementById("template");
  var audioHelp = document.getElementById("audio-help");
  var audioConsequence = document.getElementById("audio-consequence");
  var templatePresets = document.getElementById("template-presets");
  var templateExact = document.getElementById("template-exact");
  var settingsNote = document.getElementById("settings-note");
  var formStatus = document.getElementById("form-status");
  var jobsEl = document.getElementById("jobs");
  var followForm = document.getElementById("follow-form");
  var followInput = document.getElementById("follow-channel");
  var followAdd = document.getElementById("follow-add");
  var followStatus = document.getElementById("follow-status");
  var followsEl = document.getElementById("follows");
  var quitBtn = document.getElementById("quit-livegrab");

  var presets = {};
  var saveTimer = null;
  var saveGeneration = 0;
  var jobsRequest = 0;
  var jobList = [];
  var followsRequest = 0;
  var hydrating = true;
  var jobsLoaded = false;
  var followsStamp = "";
  var FOLLOW_LABELS = {
    offline: "Offline",
    live: "Live",
    checking: "Checking",
    error: "Error"
  };
  var AUDIO_HELP = {
    live: "Same audio as the player, including music. Starts from now.",
    rewind: "Rewinds to the beginning. On Twitch, this is the copy where copyrighted music is already removed."
  };
  var AUDIO_CONSEQUENCE = {
    live: "Starts from now.",
    rewind: "Starts from the beginning."
  };
  var AUDIO_LABELS = {
    live: "Live player audio",
    rewind: "From the start"
  };
  var ACTIVE = { starting: true, downloading: true, merging: true, running: true };
  var STATE_LABELS = {
    running: "Running",
    stopped: "Stopped",
    error: "Error",
    finished: "Finished",
    external: "External"
  };

  function known(value) {
    return value !== null && value !== undefined && value !== "";
  }

  function setStatus(message, kind) {
    formStatus.textContent = message || "";
    if (kind) formStatus.dataset.kind = kind;
    else delete formStatus.dataset.kind;
  }

  function setSettingsNote(message, kind) {
    settingsNote.textContent = message || "";
    if (kind) settingsNote.dataset.kind = kind;
    else delete settingsNote.dataset.kind;
  }

  function destinationValue() {
    return destinationInput.value.trim();
  }

  function destinationFromServer(data) {
    if (!data || !known(data.destination)) return "";
    return String(data.destination).trim();
  }

  function syncDestinationNeeded() {
    if (destinationValue()) {
      destinationNeeded.hidden = true;
      return;
    }
    destinationNeeded.hidden = false;
    if (destinationNeeded.dataset.kind !== "error") {
      destinationNeeded.textContent = "Choose a folder before starting.";
    }
  }

  function showFolderError(message) {
    if (!message) return;
    var text = String(message);
    var lower = text.toLowerCase();
    if (lower.indexOf("destination") === -1 && lower.indexOf("folder") === -1) return;
    if (!destinationValue()) {
      syncDestinationNeeded();
      return;
    }
    destinationNeeded.hidden = false;
    destinationNeeded.dataset.kind = "error";
    destinationNeeded.textContent = text;
  }

  function clearFolderError() {
    delete destinationNeeded.dataset.kind;
    destinationNeeded.textContent = "Choose a folder before starting.";
    syncDestinationNeeded();
  }

  function syncStart() {
    startBtn.disabled = urlInput.value.trim() === "" || destinationValue() === "";
  }

  function syncFollowAdd() {
    followAdd.disabled = followInput.value.trim() === "";
  }

  function setFollowStatus(message, kind) {
    followStatus.textContent = message || "";
    if (kind) followStatus.dataset.kind = kind;
    else delete followStatus.dataset.kind;
  }

  function updateExactTemplate() {
    templateExact.textContent = templateInput.value;
  }

  function selectedSource() {
    var picked = document.querySelector('input[name="audio_source"]:checked');
    return picked ? picked.value : "rewind";
  }

  function audioChoice(data) {
    if (data && AUDIO_HELP[data.audio_source]) return data.audio_source;
    if (data && data.live_from_start === false) return "live";
    return "rewind";
  }

  function setSource(source) {
    var value = AUDIO_HELP[source] ? source : "rewind";
    var input = document.querySelector('input[name="audio_source"][value="' + value + '"]');
    if (input) input.checked = true;
    audioHelp.textContent = AUDIO_HELP[value];
    if (audioConsequence) audioConsequence.textContent = AUDIO_CONSEQUENCE[value];
  }

  function renderPresetButtons() {
    templatePresets.replaceChildren();
    var current = templateInput.value;
    Object.keys(presets).forEach(function (label) {
      var value = presets[label];
      var button = document.createElement("button");
      button.type = "button";
      button.className = "choice" + (value === current ? " is-selected" : "");
      button.textContent = label;
      button.addEventListener("click", function () {
        templateInput.value = value;
        updateExactTemplate();
        renderPresetButtons();
        queueSave(true);
      });
      templatePresets.appendChild(button);
    });
  }

  function queueSave(immediate) {
    if (hydrating) return;
    clearTimeout(saveTimer);
    if (immediate) saveConfig();
    else saveTimer = setTimeout(saveConfig, 400);
  }

  async function readMessage(response) {
    var text = await response.text();
    if (!text) return "";
    try {
      var data = JSON.parse(text);
      if (data && known(data.error)) return String(data.error);
    } catch (err) {
      return text;
    }
    return text;
  }

  async function saveConfig() {
    var generation = ++saveGeneration;
    var body = {
      destination: destinationValue(),
      template: templateInput.value,
      audio_source: selectedSource()
    };
    try {
      var response = await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      if (generation !== saveGeneration) return;
      if (!response.ok) {
        var message = await readMessage(response);
        if (generation !== saveGeneration) return;
        setSettingsNote(message || "Could not save settings.", "error");
        showFolderError(message);
        return;
      }
      setSettingsNote("Saved.");
      clearFolderError();
    } catch (err) {
      if (generation !== saveGeneration) return;
      setSettingsNote("Could not save settings.", "error");
    }
  }

  async function loadConfig() {
    hydrating = true;
    var response = await fetch("/api/config");
    if (!response.ok) throw new Error("config");
    var data = await response.json();
    destinationInput.value = destinationFromServer(data);
    templateInput.value = known(data.template) ? String(data.template) : "";
    setSource(audioChoice(data));
    presets = data.template_presets && typeof data.template_presets === "object" ? data.template_presets : {};
    updateExactTemplate();
    renderPresetButtons();
    syncDestinationNeeded();
    syncStart();
    hydrating = false;
  }

  function formatFragment(fragment, total) {
    if (!known(fragment) && !known(total)) return "";
    if (known(fragment) && known(total)) return "fragment " + fragment + "/" + total;
    if (known(fragment)) return "fragment " + fragment;
    return "fragment —/" + total;
  }

  function formatPercent(percent) {
    if (!known(percent)) return "";
    var number = Number(percent);
    if (!Number.isFinite(number)) return String(percent);
    var rounded = Math.round(number * 10) / 10;
    return (rounded % 1 === 0 ? String(rounded) : String(rounded)) + "%";
  }

  function appendLabeled(parent, className, label, text) {
    var line = document.createElement("p");
    line.className = className;
    var name = document.createElement("span");
    name.className = "stream-label";
    name.textContent = label;
    line.appendChild(name);
    line.appendChild(document.createTextNode(text));
    parent.appendChild(line);
  }

  function streamHasDetail(stream) {
    if (!stream || typeof stream !== "object") return false;
    return known(stream.size) || known(stream.speed) || known(stream.fragment) || known(stream.fragment_total) || known(stream.percent);
  }

  function streamText(stream) {
    var parts = [];
    if (known(stream.size)) parts.push(String(stream.size));
    if (known(stream.speed)) parts.push(String(stream.speed));
    var fragment = formatFragment(stream.fragment, stream.fragment_total);
    if (fragment) parts.push(fragment);
    if (known(stream.percent)) parts.push(formatPercent(stream.percent));
    return parts.length ? parts.join(" · ") : "—";
  }

  function progressObject(job) {
    if (!job || typeof job.progress !== "object" || job.progress === null || Array.isArray(job.progress)) {
      return {};
    }
    return job.progress;
  }

  function progressEmpty(progress) {
    var keys = Object.keys(progress || {});
    if (!keys.length) return true;
    for (var i = 0; i < keys.length; i++) {
      if (known(progress[keys[i]]) && typeof progress[keys[i]] !== "object") return false;
    }
    return true;
  }

  function fieldValue(job, key) {
    if (job && known(job[key])) return job[key];
    var progress = progressObject(job);
    if (known(progress[key])) return progress[key];
    return job ? job[key] : undefined;
  }

  function isJobPayload(data) {
    if (!data || typeof data !== "object" || Array.isArray(data)) return false;
    return known(data.id) || known(data.url) || known(data.state) || known(data.status);
  }

  function normalizeJob(raw) {
    if (!isJobPayload(raw)) return null;
    var progress = progressObject(raw);
    var video = raw.video && typeof raw.video === "object" ? raw.video : progress.video;
    var audio = raw.audio && typeof raw.audio === "object" ? raw.audio : progress.audio;
    return {
      id: raw.id,
      url: fieldValue(raw, "url"),
      destination: fieldValue(raw, "destination"),
      template: fieldValue(raw, "template"),
      live_from_start: known(raw.live_from_start) ? raw.live_from_start : progress.live_from_start,
      audio_source: fieldValue(raw, "audio_source"),
      state: known(raw.state) ? raw.state : (known(raw.status) ? raw.status : progress.state),
      status: known(raw.status) ? raw.status : progress.status,
      owner: known(raw.owner) ? raw.owner : progress.owner,
      size: fieldValue(raw, "size"),
      speed: fieldValue(raw, "speed"),
      fragment: fieldValue(raw, "fragment"),
      fragment_total: fieldValue(raw, "fragment_total"),
      percent: fieldValue(raw, "percent"),
      video: video && typeof video === "object" ? video : null,
      audio: audio && typeof audio === "object" ? audio : null,
      log_tail: Array.isArray(raw.log_tail) ? raw.log_tail : [],
      output_path: fieldValue(raw, "output_path"),
      error: fieldValue(raw, "error"),
      started_at: fieldValue(raw, "started_at"),
      progress: progress
    };
  }

  function displayStatus(job) {
    var state = job && known(job.state) ? String(job.state).toLowerCase() : "";
    var status = job && known(job.status) ? String(job.status).toLowerCase() : "";
    if (state === "error" || status === "error") return "error";
    if (state === "stopped" || state === "interrupted" || status === "stopped" || status === "interrupted") return "stopped";
    if (state === "finished" || status === "finished") return "finished";
    if (state === "external" || status === "external" || (job && job.owner === "external")) return "external";
    if (ACTIVE[state] || ACTIVE[status] || status === "running" || state === "running") return "running";
    if (status) return status;
    if (state) return state;
    return "running";
  }

  function statusText(status) {
    if (STATE_LABELS[status]) return STATE_LABELS[status];
    if (!status) return "Running";
    return status.charAt(0).toUpperCase() + status.slice(1);
  }

  function audioLabel(job) {
    var source = job && known(job.audio_source) ? String(job.audio_source) : "";
    if (!AUDIO_LABELS[source]) source = job && job.live_from_start === false ? "live" : "rewind";
    return AUDIO_LABELS[source];
  }

  function mergeJob(prev, next) {
    var merged = {};
    var key;
    for (key in prev) {
      if (Object.prototype.hasOwnProperty.call(prev, key)) merged[key] = prev[key];
    }
    for (key in next) {
      if (Object.prototype.hasOwnProperty.call(next, key)) merged[key] = next[key];
    }
    ["url", "destination", "audio_source", "template", "size", "speed", "fragment", "fragment_total", "percent", "output_path", "started_at", "owner"].forEach(function (name) {
      if (!known(next[name]) && known(prev[name])) merged[name] = prev[name];
    });
    if (!known(next.state) && known(prev.state)) merged.state = prev.state;
    if (!known(next.status) && known(prev.status)) merged.status = prev.status;
    if (!known(next.live_from_start) && known(prev.live_from_start)) merged.live_from_start = prev.live_from_start;
    if (!streamHasDetail(next.video) && streamHasDetail(prev.video)) merged.video = prev.video;
    if (!streamHasDetail(next.audio) && streamHasDetail(prev.audio)) merged.audio = prev.audio;
    if (progressEmpty(next.progress) && !progressEmpty(prev.progress)) merged.progress = prev.progress;
    if (!Array.isArray(next.log_tail) || !next.log_tail.length) {
      if (Array.isArray(prev.log_tail) && prev.log_tail.length) merged.log_tail = prev.log_tail;
    }
    return merged;
  }

  function upsertJob(raw) {
    var job = normalizeJob(raw);
    if (!job) return;
    var id = known(job.id) ? String(job.id) : "";
    if (!id) {
      jobList.unshift(job);
      return;
    }
    for (var i = 0; i < jobList.length; i++) {
      if (String(jobList[i].id) === id) {
        jobList[i] = mergeJob(jobList[i], job);
        return;
      }
    }
    jobList.unshift(job);
  }

  function applyServerJobs(incoming) {
    var prior = {};
    jobList.forEach(function (job) {
      if (known(job.id)) prior[String(job.id)] = job;
    });
    var next = [];
    incoming.forEach(function (raw) {
      var job = normalizeJob(raw);
      if (!job) return;
      if (known(job.id) && prior[String(job.id)]) job = mergeJob(prior[String(job.id)], job);
      next.push(job);
    });
    jobList = next;
  }

  function renderJob(job) {
    var card = document.createElement("article");
    card.className = "job";
    var status = displayStatus(job);
    card.dataset.status = status;

    var head = document.createElement("div");
    head.className = "job-head";

    var title = document.createElement("h3");
    title.className = "job-url";
    title.textContent = known(job.url) ? String(job.url) : "Untitled grab";

    var state = document.createElement("p");
    state.className = "job-state state-" + status;
    state.textContent = statusText(status);

    head.append(title, state);
    card.appendChild(head);

    var choice = document.createElement("p");
    choice.className = "job-choice";
    choice.textContent = audioLabel(job);
    card.appendChild(choice);

    appendLabeled(card, "job-detail", "Folder", known(job.destination) ? String(job.destination) : "—");

    if (job.owner === "external" || status === "external") {
      var note = document.createElement("p");
      note.className = "elsewhere";
      note.textContent = "Already downloading elsewhere";
      card.appendChild(note);
    }

    var bits = [];
    if (known(job.size)) bits.push(String(job.size));
    if (known(job.speed)) bits.push(String(job.speed));
    var fragment = formatFragment(job.fragment, job.fragment_total);
    if (fragment) bits.push(fragment);
    var percent = formatPercent(job.percent);
    if (percent) bits.push(percent);
    if (bits.length) {
      var meta = document.createElement("p");
      meta.className = "job-meta";
      meta.textContent = bits.join(" · ");
      card.appendChild(meta);
    }

    if (streamHasDetail(job.video)) {
      appendLabeled(card, "stream", "Video", streamText(job.video));
    }
    if (streamHasDetail(job.audio)) {
      appendLabeled(card, "stream", "Audio", streamText(job.audio));
    }

    if (known(job.output_path)) {
      appendLabeled(card, "output", status === "finished" ? "Saved" : "Output", String(job.output_path));
    }

    if (known(job.error)) {
      var error = document.createElement("p");
      error.className = "error";
      error.textContent = String(job.error);
      card.appendChild(error);
    }

    if (Array.isArray(job.log_tail) && job.log_tail.length) {
      var log = document.createElement("pre");
      log.className = "log";
      log.textContent = job.log_tail.slice(-5).join("\n");
      card.appendChild(log);
    }

    if (job.owner === "livegrab" && status === "running") {
      var stop = document.createElement("button");
      stop.type = "button";
      stop.className = "stop";
      stop.textContent = "Stop";
      stop.addEventListener("click", function () {
        stopJob(job.id, stop);
      });
      card.appendChild(stop);
    }

    return card;
  }

  async function stopJob(id, button) {
    if (!known(id)) return;
    button.disabled = true;
    button.textContent = "Stopping…";
    try {
      var response = await fetch("/api/jobs/" + encodeURIComponent(id) + "/stop", {
        method: "POST"
      });
      var payload = null;
      try {
        payload = await response.json();
      } catch (parseErr) {
        payload = null;
      }
      if (!response.ok) {
        var message = payload && known(payload.error) ? String(payload.error) : "";
        setStatus(message || "Could not stop that grab.", "error");
        if (button.isConnected) {
          button.disabled = false;
          button.textContent = "Stop";
        }
        return;
      }
      if (isJobPayload(payload)) {
        upsertJob(payload);
        paintJobs();
      }
      try {
        await refreshJobs();
      } catch (refreshErr) {
        if (!jobList.length) showJobsMessage("Jobs are not available yet.");
      }
      if (button.isConnected) {
        button.disabled = false;
        button.textContent = "Stop";
      }
    } catch (err) {
      setStatus("Could not stop that grab.", "error");
      if (button.isConnected) {
        button.disabled = false;
        button.textContent = "Stop";
      }
    }
  }

  function showJobsMessage(message) {
    var empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = message;
    jobsEl.replaceChildren(empty);
  }

  function paintJobs() {
    if (!jobList.length) {
      showJobsMessage("No grabs yet.");
      return;
    }
    var visible = jobList.slice().sort(function (a, b) {
      var left = known(a.started_at) ? String(a.started_at) : "";
      var right = known(b.started_at) ? String(b.started_at) : "";
      if (left === right) return 0;
      return left < right ? 1 : -1;
    });
    var fragment = document.createDocumentFragment();
    visible.forEach(function (job) {
      fragment.appendChild(renderJob(job));
    });
    jobsEl.replaceChildren(fragment);
  }

  async function refreshJobs() {
    var request = ++jobsRequest;
    var response = await fetch("/api/jobs");
    if (request !== jobsRequest) return;
    if (!response.ok) throw new Error("jobs");
    var data = await response.json();
    if (request !== jobsRequest) return;
    var incoming = data && Array.isArray(data.jobs) ? data.jobs : [];
    jobsLoaded = true;
    applyServerJobs(incoming);
    paintJobs();
  }

  function followLabel(follow) {
    if (follow.error) return "error";
    if (follow.live_status === "is_live") return "live";
    if (follow.enabled !== false && !follow.last_checked) return "checking";
    return "offline";
  }

  function renderFollows(follows) {
    var editing = focusedFollowDestination();
    if (!follows.length) {
      var empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "No followed channels.";
      followsEl.replaceChildren(empty);
      return;
    }
    var fragment = document.createDocumentFragment();
    follows.forEach(function (follow) {
      fragment.appendChild(renderFollow(follow));
    });
    followsEl.replaceChildren(fragment);
    restoreFollowDestination(editing);
  }

  function focusedFollowDestination() {
    var active = document.activeElement;
    if (!active || !active.classList || !active.classList.contains("follow-destination")) return null;
    return {
      login: active.getAttribute("data-login") || "",
      value: active.value,
      start: active.selectionStart,
      end: active.selectionEnd
    };
  }

  function restoreFollowDestination(editing) {
    if (!editing || !editing.login) return;
    var inputs = followsEl.querySelectorAll("input.follow-destination");
    var i;
    for (i = 0; i < inputs.length; i++) {
      if (inputs[i].getAttribute("data-login") !== editing.login) continue;
      inputs[i].value = editing.value;
      inputs[i].focus();
      if (typeof editing.start === "number" && typeof editing.end === "number") {
        inputs[i].setSelectionRange(editing.start, editing.end);
      }
      return;
    }
  }

  function followAudioSource(follow) {
    var source = follow && known(follow.audio_source) ? String(follow.audio_source).trim().toLowerCase() : "";
    return AUDIO_HELP[source] ? source : "live";
  }

  function renderFollowAudio(follow) {
    var login = known(follow.login) ? String(follow.login) : "channel";
    var source = followAudioSource(follow);
    var group = document.createElement("fieldset");
    group.className = "follow-audio";

    var legend = document.createElement("legend");
    legend.textContent = "Audio";

    var sources = document.createElement("div");
    sources.className = "sources";
    sources.setAttribute("role", "radiogroup");
    sources.setAttribute("aria-label", "Audio for " + login);

    ["live", "rewind"].forEach(function (value) {
      var id = "follow-audio-" + value + "-" + login;
      var label = document.createElement("label");
      label.className = "source";
      label.htmlFor = id;
      var input = document.createElement("input");
      input.id = id;
      input.type = "radio";
      input.name = "follow_audio_" + login;
      input.value = value;
      input.checked = source === value;
      var text = document.createElement("span");
      text.textContent = AUDIO_LABELS[value];
      input.addEventListener("change", function () {
        if (!input.checked) return;
        setFollowAudio(follow.login, value, sources, source);
      });
      label.append(input, text);
      sources.appendChild(label);
    });

    var help = document.createElement("p");
    help.className = "hint";
    help.textContent = AUDIO_HELP[source];
    group.append(legend, sources, help);
    return group;
  }

  function renderFollow(follow) {
    var row = document.createElement("article");
    row.className = "follow";

    var login = document.createElement("p");
    login.className = "follow-login";
    login.textContent = known(follow.login) ? String(follow.login) : "Channel";

    var label = followLabel(follow);
    var status = document.createElement("p");
    status.className = "follow-status is-" + label;
    status.textContent = FOLLOW_LABELS[label] || label;

    var enabledLabel = document.createElement("label");
    enabledLabel.className = "check";
    var toggle = document.createElement("input");
    toggle.type = "checkbox";
    toggle.checked = follow.enabled !== false;
    toggle.setAttribute("aria-label", "Enabled");
    var enabledText = document.createElement("span");
    enabledText.textContent = "Enabled";
    enabledLabel.append(toggle, enabledText);
    toggle.addEventListener("change", function () {
      setFollowEnabled(follow.login, toggle.checked, toggle);
    });

    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove";
    remove.textContent = "Remove";
    remove.addEventListener("click", function () {
      removeFollow(follow.login, remove);
    });

    var folder = document.createElement("div");
    folder.className = "follow-folder";
    var folderLabel = document.createElement("label");
    var folderId = "follow-folder-" + (known(follow.login) ? String(follow.login) : "channel");
    folderLabel.htmlFor = folderId;
    folderLabel.textContent = "Folder";
    var folderInput = document.createElement("input");
    folderInput.id = folderId;
    folderInput.className = "follow-destination";
    folderInput.type = "text";
    folderInput.name = "destination";
    folderInput.autocomplete = "off";
    folderInput.spellcheck = false;
    folderInput.placeholder = "Folder for this channel (optional)";
    folderInput.value = known(follow.destination) ? String(follow.destination) : "";
    if (known(follow.login)) folderInput.setAttribute("data-login", String(follow.login));
    var saveFolder = document.createElement("button");
    saveFolder.type = "button";
    saveFolder.className = "save-folder";
    saveFolder.textContent = "Save";
    if (known(follow.login)) {
      saveFolder.setAttribute("aria-label", "Save folder for " + String(follow.login));
    }
    saveFolder.addEventListener("click", function () {
      setFollowDestination(follow.login, folderInput.value, saveFolder);
    });
    folderInput.addEventListener("keydown", function (event) {
      if (event.key !== "Enter") return;
      event.preventDefault();
      setFollowDestination(follow.login, folderInput.value, saveFolder);
    });
    folder.append(folderLabel, folderInput, saveFolder);

    row.append(login, status, enabledLabel, remove, renderFollowAudio(follow), folder);
    if (known(follow.error)) {
      var error = document.createElement("p");
      error.className = "error";
      error.textContent = String(follow.error);
      row.appendChild(error);
    }
    return row;
  }

  async function setFollowEnabled(login, enabled, toggle) {
    if (!known(login)) return;
    toggle.disabled = true;
    try {
      var response = await fetch("/api/follows/" + encodeURIComponent(login), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: enabled })
      });
      if (!response.ok) {
        var message = await readMessage(response);
        setFollowStatus(message || "Could not update that channel.", "error");
        toggle.checked = !enabled;
        toggle.disabled = false;
        return;
      }
      var data = await response.json();
      applyFollows(data);
      setFollowStatus(enabled ? "Channel enabled." : "Channel disabled.");
    } catch (err) {
      setFollowStatus("Could not update that channel.", "error");
      toggle.checked = !enabled;
      toggle.disabled = false;
    }
  }

  function restoreFollowAudio(radios, previous) {
    var i;
    for (i = 0; i < radios.length; i++) {
      radios[i].disabled = false;
      radios[i].checked = radios[i].value === previous;
    }
  }

  async function setFollowAudio(login, audioSource, group, previous) {
    if (!known(login)) return;
    var radios = group.querySelectorAll('input[type="radio"]');
    var i;
    for (i = 0; i < radios.length; i++) radios[i].disabled = true;
    try {
      var response = await fetch("/api/follows/" + encodeURIComponent(login), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ audio_source: audioSource })
      });
      if (!response.ok) {
        var message = await readMessage(response);
        setFollowStatus(message || "Could not save that audio choice.", "error");
        restoreFollowAudio(radios, previous);
        return;
      }
      var data = await response.json();
      applyFollows(data);
      setFollowStatus("Audio choice saved for this channel.");
    } catch (err) {
      setFollowStatus("Could not save that audio choice.", "error");
      restoreFollowAudio(radios, previous);
    }
  }

  async function setFollowDestination(login, destination, button) {
    if (!known(login)) return;
    button.disabled = true;
    try {
      var response = await fetch("/api/follows/" + encodeURIComponent(login), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ destination: destination })
      });
      if (!response.ok) {
        var message = await readMessage(response);
        setFollowStatus(message || "Could not save that folder.", "error");
        return;
      }
      var data = await response.json();
      applyFollows(data);
      if (String(destination).trim()) setFollowStatus("Folder saved for this channel.");
      else setFollowStatus("This channel will use the main folder.");
    } catch (err) {
      setFollowStatus("Could not save that folder.", "error");
    } finally {
      if (button.isConnected) button.disabled = false;
    }
  }

  async function removeFollow(login, button) {
    if (!known(login)) return;
    button.disabled = true;
    try {
      var response = await fetch("/api/follows/" + encodeURIComponent(login), {
        method: "DELETE"
      });
      if (!response.ok) {
        var message = await readMessage(response);
        setFollowStatus(message || "Could not remove that channel.", "error");
        button.disabled = false;
        return;
      }
      var data = await response.json();
      applyFollows(data);
      setFollowStatus("Channel removed.");
    } catch (err) {
      setFollowStatus("Could not remove that channel.", "error");
      button.disabled = false;
    }
  }

  function applyFollows(data) {
    followsRequest += 1;
    var follows = data && Array.isArray(data.follows) ? data.follows : [];
    followsStamp = JSON.stringify(follows);
    renderFollows(follows);
  }

  function showFollowsMessage(message) {
    var empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = message;
    followsEl.replaceChildren(empty);
  }

  async function refreshFollows() {
    var request = ++followsRequest;
    var response = await fetch("/api/follows");
    if (request !== followsRequest) return;
    if (!response.ok) throw new Error("follows");
    var data = await response.json();
    if (request !== followsRequest) return;
    var follows = Array.isArray(data.follows) ? data.follows : [];
    var stamp = JSON.stringify(follows);
    if (stamp === followsStamp) return;
    followsStamp = stamp;
    renderFollows(follows);
  }

  followForm.addEventListener("submit", async function (event) {
    event.preventDefault();
    var channel = followInput.value.trim();
    if (!channel) {
      setFollowStatus("Enter a Twitch channel.", "error");
      followInput.focus();
      syncFollowAdd();
      return;
    }
    followAdd.disabled = true;
    try {
      var response = await fetch("/api/follows", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channel: channel })
      });
      if (!response.ok) {
        var message = await readMessage(response);
        setFollowStatus(message || "Could not add that channel.", "error");
        return;
      }
      var data = await response.json();
      followInput.value = "";
      applyFollows(data);
      setFollowStatus("Channel added. Livegrab checks it on the next poll.");
    } catch (err) {
      setFollowStatus("Could not add that channel.", "error");
    } finally {
      syncFollowAdd();
    }
  });

  followInput.addEventListener("input", syncFollowAdd);

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    var url = urlInput.value.trim();
    var destination = destinationValue();
    if (!destination) {
      clearFolderError();
      destinationInput.focus();
      syncStart();
      return;
    }
    if (!url) {
      setStatus("Enter a URL before starting a grab.", "error");
      urlInput.focus();
      syncStart();
      return;
    }
    startBtn.disabled = true;
    try {
      var response = await fetch("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: url,
          destination: destination,
          template: templateInput.value,
          audio_source: selectedSource()
        })
      });
      if (!response.ok) {
        var message = await readMessage(response);
        setStatus(message || "Could not start the grab.", "error");
        showFolderError(message);
        return;
      }
      var job = null;
      try {
        job = await response.json();
      } catch (parseErr) {
        job = null;
      }
      if (isJobPayload(job)) {
        upsertJob(job);
        paintJobs();
      }
      var started = normalizeJob(job);
      var startedStatus = started ? displayStatus(started) : "";
      if (startedStatus === "error") {
        setStatus(started && known(started.error) ? String(started.error) : "Could not start the grab.", "error");
      } else if (startedStatus === "external") {
        urlInput.value = "";
        setStatus("Already downloading elsewhere.");
      } else {
        urlInput.value = "";
        setStatus("Grab started.");
      }
      try {
        await refreshJobs();
      } catch (refreshErr) {
        if (!jobList.length) showJobsMessage("Jobs are not available yet.");
      }
    } catch (err) {
      setStatus("Could not start the grab.", "error");
    } finally {
      syncStart();
    }
  });

  destinationInput.addEventListener("input", function () {
    clearFolderError();
    syncStart();
    queueSave(false);
  });

  templateInput.addEventListener("input", function () {
    updateExactTemplate();
    renderPresetButtons();
    queueSave(false);
  });

  document.querySelectorAll('input[name="audio_source"]').forEach(function (input) {
    input.addEventListener("change", function () {
      setSource(selectedSource());
      queueSave(true);
    });
  });

  urlInput.addEventListener("input", syncStart);

  if (quitBtn) {
    quitBtn.addEventListener("click", function () {
      var confirmed = window.confirm(
        "Quit Livegrab? Running downloads will be stopped, then Livegrab will exit."
      );
      if (!confirmed) return;
      quitBtn.disabled = true;
      fetch("/api/shutdown", { method: "POST" }).catch(function () {
        // The process exits after the response, so the connection may drop.
      });
    });
  }

  async function init() {
    syncDestinationNeeded();
    syncStart();
    syncFollowAdd();
    try {
      await loadConfig();
    } catch (err) {
      hydrating = false;
      syncDestinationNeeded();
      syncStart();
      setSettingsNote("Settings are not available yet.", "error");
    }
    try {
      await refreshJobs();
    } catch (err) {
      showJobsMessage("Jobs are not available yet.");
    }
    try {
      await refreshFollows();
    } catch (err) {
      showFollowsMessage("Followed channels are not available yet.");
    }
    setInterval(function () {
      refreshJobs().catch(function () {
        if (!jobsLoaded && !jobList.length) showJobsMessage("Jobs are not available yet.");
      });
    }, 2000);
    setInterval(function () {
      refreshFollows().catch(function () {
        if (!followsStamp) showFollowsMessage("Followed channels are not available yet.");
      });
    }, 15000);
  }

  init();
})();
