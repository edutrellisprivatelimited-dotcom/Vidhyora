/* Shared by /chats and the dashboard's chat page: drawing rich messages
   (photos, videos, files, locations, calls) and voice/video calls (WebRTC).
   Everything is built with DOM methods — message text is never parsed as HTML. */
(function () {
  'use strict';

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function mapSrc(lat, lng) {
    var d = 0.006;
    return 'https://www.openstreetmap.org/export/embed.html?bbox=' + (lng - d) + ',' + (lat - d) + ',' +
      (lng + d) + ',' + (lat + d) + '&layer=mapnik&marker=' + lat + ',' + lng;
  }

  function liveLabel(loc) {
    if (!loc.live_until) return 'Location';
    if (!loc.live) return 'Live location ended';
    var mins = Math.max(1, Math.round((new Date(loc.live_until) - new Date()) / 60000));
    return 'Live location · ' + (mins >= 60 ? Math.round(mins / 60) + ' h' : mins + ' min') + ' left';
  }

  // Fills a bubble's content area for message m.
  function renderBody(box, m) {
    box.textContent = '';
    if (m.kind === 'image' && m.url) {
      var img = el('img', 'att-img');
      img.src = m.url; img.alt = m.name || 'Photo'; img.loading = 'lazy'; img.draggable = false;
      img.addEventListener('click', function () { openViewer(m.url, m.name); });
      img.addEventListener('contextmenu', function (e) { if (!api.canSave) e.preventDefault(); });
      box.appendChild(img);
      if (api.canSave) box.appendChild(saveButton(m.url, m.name || 'photo'));
    } else if (m.kind === 'video' && m.url) {
      var v = el('video', 'att-video');
      v.src = m.url; v.controls = true; v.preload = 'metadata'; v.playsInline = true;
      v.setAttribute('controlsList', 'nodownload noplaybackrate'); v.disablePictureInPicture = true;
      v.addEventListener('contextmenu', function (e) { if (!api.canSave) e.preventDefault(); });
      box.appendChild(v);
      if (api.canSave) box.appendChild(saveButton(m.url, m.name || 'video'));
    } else if (m.kind === 'file' && m.url) {
      var a = el('a', 'att-file');
      a.href = m.url; a.target = '_blank'; a.rel = 'noopener';
      var ic = el('i', 'fas fa-file-lines'); a.appendChild(ic);
      a.appendChild(el('span', 'att-file-name', m.name || 'File'));
      a.appendChild(el('i', 'fas fa-download att-file-dl'));
      box.appendChild(a);
    } else if (m.kind === 'location' && m.lat != null) {
      var wrap = el('div', 'att-loc');
      wrap.dataset.locId = m.id;
      var frame = el('iframe', 'att-map');
      frame.src = mapSrc(m.lat, m.lng); frame.loading = 'lazy'; frame.title = 'Map';
      frame.setAttribute('referrerpolicy', 'no-referrer');
      wrap.appendChild(frame);
      var row = el('div', 'att-loc-row');
      var label = el('span', 'att-loc-label' + (m.live ? ' live' : ''));
      label.textContent = liveLabel(m);
      row.appendChild(label);
      var open = el('a', 'att-loc-open', 'Open in Maps');
      open.href = 'https://www.google.com/maps?q=' + m.lat + ',' + m.lng; open.target = '_blank'; open.rel = 'noopener';
      row.appendChild(open);
      wrap.appendChild(row);
      box.appendChild(wrap);
      wrap._loc = { lat: m.lat, lng: m.lng };
    } else if (m.kind === 'call') {
      var call = el('div', 'att-call');
      var missed = /missed|declined/i.test(m.body);
      call.appendChild(el('i', 'fas ' + (/video/i.test(m.body) ? 'fa-video' : 'fa-phone') + (missed ? ' missed' : '')));
      call.appendChild(el('span', '', m.body));
      box.appendChild(call);
      return;
    }
    if (m.body && m.kind !== 'location') box.appendChild(el('div', 'msg-text', m.body));
  }

  // Moves live-location pins already on screen.
  function updateLocations(root, locations) {
    (locations || []).forEach(function (loc) {
      var wrap = root.querySelector('.att-loc[data-loc-id="' + loc.id + '"]');
      if (!wrap) return;
      var label = wrap.querySelector('.att-loc-label');
      label.textContent = liveLabel(loc);
      label.classList.toggle('live', !!loc.live);
      if (!wrap._loc || wrap._loc.lat !== loc.lat || wrap._loc.lng !== loc.lng) {
        wrap._loc = { lat: loc.lat, lng: loc.lng };
        wrap.querySelector('.att-map').src = mapSrc(loc.lat, loc.lng);
        wrap.querySelector('.att-loc-open').href = 'https://www.google.com/maps?q=' + loc.lat + ',' + loc.lng;
      }
    });
  }

  // Staff only: a "Save" button that downloads the original file.
  function downloadUrl(url) { return url + (url.indexOf('?') === -1 ? '?' : '&') + 'download=1'; }
  function saveButton(url, name) {
    var a = el('a', 'att-save');
    a.href = downloadUrl(url); a.download = name; a.title = 'Save to this computer';
    a.appendChild(el('i', 'fas fa-download')); a.appendChild(el('span', '', 'Save'));
    a.addEventListener('click', function (e) { e.stopPropagation(); });
    return a;
  }

  var viewer;
  function openViewer(url, name) {
    if (!viewer) {
      viewer = el('div', 'att-viewer');
      viewer.appendChild(el('img'));
      var close = el('button', 'att-viewer-close'); close.innerHTML = '<i class="fas fa-xmark"></i>';
      close.setAttribute('aria-label', 'Close');
      viewer.appendChild(close);
      viewer.addEventListener('click', function () { viewer.classList.remove('open'); });
      viewer.addEventListener('contextmenu', function (e) { if (!api.canSave) e.preventDefault(); });
      document.body.appendChild(viewer);
    }
    var old = viewer.querySelector('.att-save');
    if (old) old.remove();
    if (api.canSave) { var save = saveButton(url, name || 'photo'); save.classList.add('att-viewer-save'); viewer.appendChild(save); }
    viewer.querySelector('img').src = url;
    viewer.classList.add('open');
  }

  // ── calls ──────────────────────────────────────────────────────────────────
  // opts: { url, side: 'user'|'admin', peerName, csrf, onError(text) }
  function Calls(opts) {
    var self = this;
    var pc = null, local = null, call = null, iceSeen = 0, fast = null, timer = null, ringTone = null;
    var pendingIce = [];
    var ui = buildUi();

    function buildUi() {
      // The dashboard swaps pages in place, so an earlier page's (idle) call
      // screen may still be here.
      document.querySelectorAll('.call-overlay:not(.open)').forEach(function (old) { old.remove(); });
      var o = el('div', 'call-overlay');
      o.innerHTML =
        '<video class="call-remote" autoplay playsinline></video>' +
        '<video class="call-local" autoplay playsinline muted></video>' +
        '<div class="call-info"><div class="call-avatar"><i class="fas fa-user"></i></div>' +
        '<div class="call-name"></div><div class="call-state"></div></div>' +
        '<div class="call-bar">' +
        '<button class="call-btn mute" title="Mute"><i class="fas fa-microphone"></i></button>' +
        '<button class="call-btn cam" title="Camera"><i class="fas fa-video"></i></button>' +
        '<button class="call-btn accept" title="Answer"><i class="fas fa-phone"></i></button>' +
        '<button class="call-btn end" title="End"><i class="fas fa-phone-slash"></i></button></div>';
      document.body.appendChild(o);
      o.querySelector('.call-name').textContent = opts.peerName;
      o.querySelector('.end').addEventListener('click', function () { self.hangUp(); });
      o.querySelector('.accept').addEventListener('click', function () { self.answer(); });
      o.querySelector('.mute').addEventListener('click', function () {
        if (!local) return;
        var t = local.getAudioTracks()[0]; if (!t) return;
        t.enabled = !t.enabled; this.classList.toggle('off', !t.enabled);
      });
      o.querySelector('.cam').addEventListener('click', function () {
        if (!local) return;
        var t = local.getVideoTracks()[0]; if (!t) return;
        t.enabled = !t.enabled; this.classList.toggle('off', !t.enabled);
      });
      return o;
    }

    function show(state, text) {
      ui.className = 'call-overlay open ' + state + ' ' + (call && call.kind === 'video' ? 'is-video' : 'is-audio');
      ui.querySelector('.call-state').textContent = text;
    }

    function send(data) {
      return fetch(opts.url, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': opts.csrf }, body: JSON.stringify(data)
      }).then(function (r) { return r.json(); });
    }

    function media(kind) {
      if (!navigator.mediaDevices || !window.RTCPeerConnection) {
        return Promise.reject(new Error('Calls need a modern browser on a secure (https) page.'));
      }
      return navigator.mediaDevices.getUserMedia({ audio: true, video: kind === 'video' ? { facingMode: 'user' } : false })
        .catch(function () { throw new Error('Allow microphone' + (kind === 'video' ? ' and camera' : '') + ' access to call.'); });
    }

    function makePeer(servers) {
      pc = new RTCPeerConnection({ iceServers: servers });
      local.getTracks().forEach(function (t) { pc.addTrack(t, local); });
      ui.querySelector('.call-local').srcObject = local;
      pc.ontrack = function (e) { ui.querySelector('.call-remote').srcObject = e.streams[0]; };
      pc.onicecandidate = function (e) {
        if (e.candidate && call) send({ action: 'ice', id: call.id, candidate: e.candidate.toJSON() });
      };
      pc.onconnectionstatechange = function () {
        if (pc && pc.connectionState === 'connected') startTimer();
        if (pc && pc.connectionState === 'failed') { opts.onError('The call could not connect on this network.'); self.hangUp(); }
      };
    }

    function startTimer() {
      if (timer) return;
      var t0 = Date.now();
      show('active', '0:00');
      timer = setInterval(function () {
        var s = Math.floor((Date.now() - t0) / 1000);
        ui.querySelector('.call-state').textContent = Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2);
      }, 1000);
    }

    function addIce(list) {
      (list || []).slice(iceSeen).forEach(function (c) {
        if (pc && pc.remoteDescription) pc.addIceCandidate(c).catch(function () {});
        else pendingIce.push(c);
      });
      iceSeen = Math.max(iceSeen, (list || []).length);
    }

    function flushIce() {
      pendingIce.splice(0).forEach(function (c) { pc.addIceCandidate(c).catch(function () {}); });
    }

    function pollFast() {
      if (fast) return;
      fast = setInterval(function () {
        fetch(opts.url, { credentials: 'same-origin' }).then(function (r) { return r.json(); })
          .then(function (res) { if (res.ok) self.sync(res.call, res.ice_servers); }).catch(function () {});
      }, 1000);
    }

    function ring(on) {
      try {
        if (on && !ringTone) {
          var ctx = new (window.AudioContext || window.webkitAudioContext)();
          var osc = ctx.createOscillator(), gain = ctx.createGain();
          osc.frequency.value = 440; gain.gain.value = 0;
          osc.connect(gain); gain.connect(ctx.destination); osc.start();
          var beat = setInterval(function () {
            gain.gain.setValueAtTime(0.08, ctx.currentTime);
            gain.gain.setValueAtTime(0, ctx.currentTime + 0.4);
          }, 1500);
          ringTone = { ctx: ctx, beat: beat };
        } else if (!on && ringTone) {
          clearInterval(ringTone.beat); ringTone.ctx.close(); ringTone = null;
        }
      } catch (e) {}
    }

    function cleanup(text) {
      ring(false);
      clearInterval(fast); fast = null;
      clearInterval(timer); timer = null;
      if (pc) { pc.close(); pc = null; }
      if (local) { local.getTracks().forEach(function (t) { t.stop(); }); local = null; }
      ui.querySelector('.call-remote').srcObject = null;
      iceSeen = 0; pendingIce = [];
      if (text) { show('ended', text); setTimeout(function () { ui.className = 'call-overlay'; }, 1800); }
      else ui.className = 'call-overlay';
      call = null;
    }

    this.start = function (kind) {
      if (call) return;
      call = { kind: kind };
      show('calling', 'Calling…');
      media(kind).then(function (stream) {
        local = stream;
        return fetch(opts.url, { credentials: 'same-origin' }).then(function (r) { return r.json(); });
      }).then(function (res) {
        makePeer(res.ice_servers);
        return pc.createOffer().then(function (offer) { return pc.setLocalDescription(offer).then(function () { return offer; }); });
      }).then(function (offer) {
        return send({ action: 'start', kind: kind, offer: { type: offer.type, sdp: offer.sdp } });
      }).then(function (res) {
        if (!res.ok) throw new Error(res.error || 'Could not start the call.');
        call = res.call; show('calling', 'Ringing…'); pollFast();
      }).catch(function (err) { cleanup(); opts.onError(err.message); });
    };

    this.answer = function () {
      if (!call || !call.offer) return;
      ring(false);
      show('connecting', 'Connecting…');
      media(call.kind).then(function (stream) {
        local = stream;
        return fetch(opts.url, { credentials: 'same-origin' }).then(function (r) { return r.json(); });
      }).then(function (res) {
        makePeer(res.ice_servers);
        return pc.setRemoteDescription(call.offer);
      }).then(function () {
        flushIce();
        return pc.createAnswer();
      }).then(function (answer) {
        return pc.setLocalDescription(answer).then(function () {
          return send({ action: 'answer', id: call.id, answer: { type: answer.type, sdp: answer.sdp } });
        });
      }).then(function (res) {
        if (!res.ok) throw new Error(res.error || 'Could not answer.');
        pollFast();
      }).catch(function (err) {
        if (call) send({ action: 'decline', id: call.id });
        cleanup(); opts.onError(err.message);
      });
    };

    this.hangUp = function () {
      if (call && call.id) send({ action: call.status === 'ringing' && call.caller !== opts.side ? 'decline' : 'end', id: call.id });
      cleanup('Call ended');
    };

    // Called with the server's view of the call on every poll.
    this.sync = function (state) {
      if (!state || state.status === 'ended') {
        if (call && call.id && (!state || state.id === call.id)) {
          var reason = state && state.reason;
          cleanup(reason === 'declined' ? 'Call declined' : reason === 'missed' ? 'No answer' : 'Call ended');
        }
        return;
      }
      if (!call) {
        if (state.status === 'ringing' && state.caller !== opts.side) {
          call = state;
          show('incoming', 'Incoming ' + (state.kind === 'video' ? 'video' : 'voice') + ' call…');
          ring(true);
          pollFast();
        }
        return;
      }
      if (call.id && call.id !== state.id) return;
      var wasRinging = call.status === 'ringing';
      call = Object.assign(call, state);
      if (state.answer && pc && !pc.currentRemoteDescription && state.caller === opts.side) {
        pc.setRemoteDescription(state.answer).then(flushIce).catch(function () {});
        show('connecting', 'Connecting…');
      }
      if (wasRinging && state.status === 'active' && state.caller !== opts.side && !pc) cleanup();   // answered elsewhere
      addIce(state.ice);
    };
  }

  // Emoji picker: the full Unicode set with categories, search, skin tones and
  // recently used, loaded only the first time someone opens it.
  var PICKER_SRC = 'https://cdn.jsdelivr.net/npm/emoji-picker-element@1.21.3/index.js';
  var pickerReady = null;
  function loadPicker() {
    if (!pickerReady) {
      pickerReady = new Promise(function (resolve, reject) {
        var s = document.createElement('script');
        s.type = 'module'; s.src = PICKER_SRC;
        s.onload = function () { customElements.whenDefined('emoji-picker').then(resolve); };
        s.onerror = function () { pickerReady = null; reject(); };
        document.head.appendChild(s);
      });
    }
    return pickerReady;
  }
  function insertAtCursor(box, text) {
    var start = box.selectionStart == null ? box.value.length : box.selectionStart;
    var end = box.selectionEnd == null ? start : box.selectionEnd;
    if (box.maxLength > 0 && box.value.length - (end - start) + text.length > box.maxLength) return;
    box.value = box.value.slice(0, start) + text + box.value.slice(end);
    box.selectionStart = box.selectionEnd = start + text.length;
    box.dispatchEvent(new Event('input', { bubbles: true }));
  }
  function emojiPicker(button, box) {
    var pop = null;
    function place() {
      var r = button.getBoundingClientRect(), w = Math.min(352, window.innerWidth - 16);
      pop.style.width = w + 'px';
      pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8)) + 'px';
      pop.style.bottom = (window.innerHeight - r.top + 10) + 'px';
    }
    function close() { if (pop) pop.classList.remove('open'); button.classList.remove('on'); }
    button.addEventListener('click', function (e) {
      e.stopPropagation();
      if (pop && pop.classList.contains('open')) return close();
      button.classList.add('on');
      loadPicker().then(function () {
        if (!pop) {
          pop = document.createElement('div'); pop.className = 'emoji-pop';
          var picker = document.createElement('emoji-picker');
          picker.className = 'light';
          picker.addEventListener('emoji-click', function (ev) { insertAtCursor(box, ev.detail.unicode); });
          pop.appendChild(picker);
          pop.addEventListener('click', function (ev) { ev.stopPropagation(); });
          document.body.appendChild(pop);
        }
        place(); pop.classList.add('open');
      }).catch(function () { button.classList.remove('on'); alert('Emojis could not load. Please check your internet and try again.'); });
    });
    document.addEventListener('click', close);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
    window.addEventListener('resize', function () { if (pop && pop.classList.contains('open')) place(); });
  }

  var api = window.SupportChat = { canSave: false, renderBody: renderBody, updateLocations: updateLocations, Calls: Calls, emojiPicker: emojiPicker };
})();
