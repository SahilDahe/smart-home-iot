'use strict';


/* ============================================================
   CONFIG
============================================================ */

const CFG = {

  POLL_MS: 1000,

  WAVE_MS: 90,

  FPS_MS: 1000,

  CAMERA_MAX_RETRY: 8

};


/* ============================================================
   APP STATE
============================================================ */

const APP = {

  socket: null,

  cameraOk: false,

  cameraRetries: 0,

  fpsTimer: null,

  waveTimer: null,

  waveActive: false,

  timeline: [],

  devices: {},

  gesture: 'NONE',

  lastGesture: 'NONE',

  lastStateKey: '',

  fabOpen: false,

  sosActive: false

};


const $ =
  id =>
    document.getElementById(id);


const $$ =
  selector =>
    document.querySelectorAll(selector);


/* ============================================================
   HELPERS
============================================================ */

function sleep(ms) {

  return new Promise(
    resolve =>
      setTimeout(resolve, ms)
  );

}


function now() {

  return new Date().toLocaleTimeString(
    'en-IN',
    {
      hour12: false,
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit'
    }
  );

}


function setText(
  id,
  text
) {

  const element =
    $(id);

  if (element) {

    element.textContent =
      text;
  }

}


function escapeHtml(value) {

  return String(value)

    .replaceAll(
      '&',
      '&amp;'
    )

    .replaceAll(
      '<',
      '&lt;'
    )

    .replaceAll(
      '>',
      '&gt;'
    )

    .replaceAll(
      '"',
      '&quot;'
    )

    .replaceAll(
      "'",
      '&#039;'
    );
}


/* ============================================================
   BOOT
============================================================ */

async function runLoader() {

  const loader =
    $('loader');

  const progress =
    $('loader-bar-fill');


  const values = [
    18,
    40,
    65,
    100
  ];


  for (
    const value of values
  ) {

    if (progress) {

      progress.style.width =
        `${value}%`;
    }

    await sleep(220);
  }


  await sleep(350);


  if (loader) {

    loader.classList.add(
      'hidden'
    );
  }

}


/* ============================================================
   CLOCK
============================================================ */

function initClock() {

  function tick() {

    const d =
      new Date();


    const time =
      $('nav-time');

    const date =
      $('nav-date');


    if (time) {

      time.textContent =
        d.toLocaleTimeString(
          'en-IN',
          {
            hour12: false,
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit'
          }
        );
    }


    if (date) {

      date.textContent =
        d.toLocaleDateString(
          'en-IN',
          {
            weekday: 'short',
            month: 'short',
            day: 'numeric',
            year: 'numeric'
          }
        );
    }

  }


  tick();

  setInterval(
    tick,
    1000
  );

}


/* ============================================================
   SOCKET.IO
============================================================ */

function initSocket() {

  APP.socket =
    io({
      transports: [
        'websocket',
        'polling'
      ]
    });


  APP.socket.on(
    'connect',
    () => {

      setPill(
        'pill-wifi',
        true
      );

      log(
        'Realtime connection established',
        'info',
        'radio'
      );

    }
  );


  APP.socket.on(
    'disconnect',
    () => {

      setPill(
        'pill-wifi',
        false
      );

      log(
        'Realtime connection lost',
        'warn',
        'wifi-off'
      );

    }
  );


  APP.socket.on(
    'state',
    state => {

      applyState(
        state
      );

    }
  );

}


/* ============================================================
   STATE APPLICATION
============================================================ */

function applyState(
  data
) {

  if (!data)
    return;


  const key =
    JSON.stringify(data);


  if (
    key === APP.lastStateKey
  ) {

    return;
  }


  APP.lastStateKey =
    key;


  [
    'light1',
    'light2',
    'fan',
    'pump'
  ].forEach(
    device => {

      if (
        data[device] !== undefined
      ) {

        applyDevice(
          device,
          data[device]
        );
      }

    }
  );


  if (
    data.door !== undefined
  ) {

    applyDoor(
      data.door
    );
  }


  if (
    data.temperature !== undefined
  ) {

    setText(
      'stat-temp',
      data.temperature
    );
  }


  if (
    data.humidity !== undefined
  ) {

    setText(
      'stat-humidity',
      data.humidity
    );
  }


  if (
    data.esp !== undefined
  ) {

    setText(
      'stat-esp',
      data.esp
    );

    setPill(
      'pill-esp',
      data.esp === 'ONLINE'
    );
  }


  if (
    data.motion !== undefined
  ) {

    setText(
      'stat-motion',
      data.motion
    );
  }


  if (
    data.alert !== undefined
  ) {

    applySecurity(
      data.alert
    );
  }


  if (
    data.gesture !== undefined
  ) {

    updateGestureUI(
      data
    );
  }


  if (
    data.sos !== undefined
  ) {

    applySOS(
      data.sos
    );
  }


  updateDeviceCount();

}


/* ============================================================
   DEVICES
============================================================ */

const DEVICE_NAMES = {

  light1:
    'Living Room Light',

  light2:
    'Bedroom Light',

  fan:
    'Smart Fan',

  pump:
    'Water Pump'

};


const DEVICE_ICONS = {

  light1:
    'lightbulb',

  light2:
    'lamp-desk',

  fan:
    'fan',

  pump:
    'droplet'

};


function applyDevice(
  device,
  value
) {

  const isOn =
    value === 'ON';


  const previous =
    APP.devices[device];


  const changed =
    previous !== value;


  APP.devices[device] =
    value;


  const row =
    $(`row-${device}`);


  const label =
    $(`label-${device}`);


  const toggle =
    $(`toggle-${device}`);


  if (!row)
    return;


  row.classList.toggle(
    'on',
    isOn
  );


  if (label) {

    label.textContent =
      value;
  }


  if (
    toggle
    &&
    toggle.checked !== isOn
  ) {

    toggle.checked =
      isOn;
  }


  if (
    device === 'fan'
  ) {

    const icon =
      row.querySelector(
        '.device-icon'
      );


    if (icon) {

      icon.style.animation =
        isOn
          ? 'fanSpin 1.3s linear infinite'
          : '';
    }

  }


  if (changed) {

    log(
      `${DEVICE_NAMES[device]} ${value}`,
      isOn
        ? 'on'
        : 'off',
      DEVICE_ICONS[device]
    );
  }

}


/* ============================================================
   DOOR
============================================================ */

function applyDoor(
  value
) {

  const unlocked =
    value === 'UNLOCKED'
    ||
    value === 'UNLOCK';


  const previous =
    APP.devices.door;


  const changed =
    previous !== value;


  APP.devices.door =
    value;


  const row =
    $('row-door');

  const label =
    $('label-door');

  const toggle =
    $('toggle-door');

  const stat =
    $('stat-door');


  if (row) {

    row.classList.toggle(
      'on',
      unlocked
    );
  }


  if (label) {

    label.textContent =
      unlocked
        ? 'UNLOCKED'
        : 'LOCKED';
  }


  if (toggle) {

    toggle.checked =
      unlocked;
  }


  if (stat) {

    stat.textContent =
      unlocked
        ? 'UNLOCKED'
        : 'LOCKED';

    stat.className =
      unlocked
        ? 'warning'
        : 'success';
  }


  if (
    changed
  ) {

    log(
      unlocked
        ? 'Door unlocked'
        : 'Door locked',
      unlocked
        ? 'warn'
        : 'on',
      'lock'
    );
  }

}


/* ============================================================
   DEVICE COUNT
============================================================ */

function updateDeviceCount() {

  const active =
    [
      'light1',
      'light2',
      'fan',
      'pump'
    ]
      .filter(
        device =>
          APP.devices[device]
          ===
          'ON'
      )
      .length
    +
    (
      APP.devices.door
      === 'UNLOCKED'
      ||
      APP.devices.door
      === 'UNLOCK'
        ? 1
        : 0
    );


  setText(
    'devices-active-count',
    `${active} of 5 active`
  );

}


/* ============================================================
   GESTURE UI
============================================================ */

function updateGestureUI(
  data
) {

  const gesture =
    data.gesture
    ||
    'NONE';


  const progress =
    Number(
      data.gesture_progress
      ||
      0
    );


  const phase =
    data.gesture_phase
    ||
    'READY';


  const action =
    data.gesture_action
    ||
    'Show a gesture to begin';


  APP.gesture =
    gesture;


  setText(
    'gesture-name',
    gesture
  );


  setText(
    'overlay-gesture',
    gesture
  );


  setText(
    'stat-gesture',
    gesture
  );


  setText(
    'gesture-conf-pct',
    `${progress}%`
  );


  setText(
    'gesture-action',
    action
  );


  const fill =
    $('gesture-conf-fill');


  if (fill) {

    fill.style.width =
      `${progress}%`;
  }


  const phaseLabel =
    $('gesture-phase-label');


  if (phaseLabel) {

    const labels = {

      READY:
        'Ready for input',

      HOLD:
        'Hold the gesture...',

      TRIGGERED:
        'Action executed',

      IGNORED:
        'Gesture not assigned'

    };


    phaseLabel.textContent =
      labels[phase]
      ||
      'Ready for input';
  }


  document
    .querySelectorAll(
      '.gesture-chip'
    )
    .forEach(
      chip => {

        chip.classList.toggle(
          'active',
          chip.dataset.gesture
          ===
          gesture
        );

      }
    );


  if (
    phase === 'HOLD'
    &&
    progress > 0
  ) {

    setText(
      'gesture-last',
      `${gesture} / ${progress}%`
    );

    animateWave(
      true
    );

  } else {

    animateWave(
      false
    );
  }


}


/* ============================================================
   SECURITY
============================================================ */

function applySecurity(
  value
) {

  const badge =
    $('security-badge');

  const sub =
    $('security-sub');

  const module =
    $('security-module');


  if (!badge)
    return;


  badge.className =
    'security-badge';


  if (
    value === 'NORMAL'
  ) {

    badge.classList.add(
      'normal'
    );

    badge.textContent =
      'NORMAL';


    if (sub) {

      sub.textContent =
        'All systems normal';
    }


    if (module) {

      module.classList.remove(
        'sos-active'
      );
    }


  } else if (
    value === 'MOTION'
  ) {

    badge.classList.add(
      'warning'
    );

    badge.textContent =
      'MOTION';


    if (sub) {

      sub.textContent =
        'Motion detected';
    }


  } else if (
    value === 'SOS'
  ) {

    applySOS(
      'ACTIVE'
    );


  } else {

    badge.classList.add(
      'danger'
    );

    badge.textContent =
      value;


    if (sub) {

      sub.textContent =
        `Security alert: ${value}`;
    }

  }

}


/* ============================================================
   SOS
============================================================ */

function applySOS(
  value
) {

  const active =
    value === 'ACTIVE';


  if (
    active === APP.sosActive
  ) {
    return;
  }


  APP.sosActive =
    active;


  const overlay =
    $('sos-overlay');

  const badge =
    $('security-badge');

  const sub =
    $('security-sub');

  const module =
    $('security-module');


  if (active) {

    if (overlay) {

      overlay.classList.add(
        'active'
      );

      overlay.setAttribute(
        'aria-hidden',
        'false'
      );
    }


    if (badge) {

      badge.className =
        'security-badge sos';

      badge.textContent =
        'SOS';
    }


    if (sub) {

      sub.textContent =
        'Emergency mode active';
    }


    if (module) {

      module.classList.add(
        'sos-active'
      );
    }


    log(
      'SOS ACTIVATED',
      'danger',
      'triangle-alert'
    );


  } else {

    if (overlay) {

      overlay.classList.remove(
        'active'
      );

      overlay.setAttribute(
        'aria-hidden',
        'true'
      );
    }


    if (badge) {

      badge.className =
        'security-badge normal';

      badge.textContent =
        'NORMAL';
    }


    if (sub) {

      sub.textContent =
        'All systems normal';
    }


    if (module) {

      module.classList.remove(
        'sos-active'
      );
    }

  }

}


/* ============================================================
   CONTROL API
============================================================ */

function toggleDevice(
  device,
  checked
) {

  sendControl(
    device,
    checked
      ? 'ON'
      : 'OFF'
  );

}


function toggleDoor(
  unlocked
) {

  sendControl(
    'door',
    unlocked
      ? 'UNLOCK'
      : 'LOCK'
  );

}


function sendControl(
  device,
  action
) {

  fetch(
    `/api/control/${device}/${action}`,
    {
      method: 'POST'
    }
  )

    .then(
      response =>
        response.json()
    )

    .then(
      data => {

        if (!data.success) {

          log(
            data.error
              ||
              'Command failed',
            'danger',
            'alert-circle'
          );
        }

      }
    )

    .catch(
      () => {

        log(
          'Unable to reach control server',
          'danger',
          'wifi-off'
        );

      }
    );

}


/* ============================================================
   SOS ACKNOWLEDGE
============================================================ */

function initSOS() {

  const button =
    $('sos-dismiss');


  if (!button)
    return;


  button.addEventListener(
    'click',
    () => {

      fetch(
        '/api/sos/reset',
        {
          method: 'POST'
        }
      )

        .then(
          response =>
            response.json()
        )

        .then(
          data => {

            if (
              data.success
              &&
              data.state
            ) {

              applyState(
                data.state
              );
            }

          }
        )

        .catch(
          () => {

            log(
              'Unable to acknowledge SOS',
              'danger',
              'alert-circle'
            );

          }
        );

    }
  );

}


/* ============================================================
   TIMELINE
============================================================ */

const ICON_MAP = {

  lightbulb:
    'lightbulb',

  lamp:
    'lamp-desk',

  fan:
    'fan',

  droplet:
    'droplet',

  lock:
    'lock',

  check:
    'check-circle-2',

  info:
    'info',

  warn:
    'triangle-alert',

  danger:
    'octagon',

  radio:
    'radio',

  'wifi-off':
    'wifi-off',

  activity:
    'activity',

  camera:
    'camera',

  'alert-circle':
    'alert-circle',

  'triangle-alert':
    'triangle-alert'

};


function log(
  message,
  type = 'info',
  icon = 'info'
) {

  const timeline =
    $('timeline');


  if (!timeline)
    return;


  const empty =
    $('timeline-empty');


  if (empty) {

    empty.remove();
  }


  const item =
    document.createElement(
      'div'
    );


  const safeIcon =
    ICON_MAP[icon]
    ||
    icon;


  item.className =
    'timeline-item';


  item.innerHTML = `

    <div class="tl-icon">

      <i data-lucide="${safeIcon}"></i>

    </div>

    <span class="tl-text">

      ${escapeHtml(message)}

    </span>

    <span class="tl-time">

      ${now()}

    </span>

  `;


  timeline.insertBefore(
    item,
    timeline.firstChild
  );


  if (window.lucide) {

    lucide.createIcons({
      nodes: [item]
    });
  }


  while (
    timeline.children.length > 60
  ) {

    timeline.lastChild.remove();
  }

}


/* ============================================================
   WAVEFORM
============================================================ */

function animateWave(
  active
) {

  APP.waveActive =
    active;


  if (!APP.waveTimer) {

    tickWave();
  }

}


function tickWave() {

  const bars =
    $$('.gesture-wave span');


  bars.forEach(
    bar => {

      if (
        APP.waveActive
      ) {

        bar.style.height =
          `${8 + Math.random() * 82}%`;

        bar.style.opacity =
          `${0.35 + Math.random() * 0.65}`;

      } else {

        bar.style.height =
          '4px';

        bar.style.opacity =
          '.18';
      }

    }
  );


  APP.waveTimer =
    setTimeout(
      () => {

        APP.waveTimer =
          null;

        tickWave();

      },

      APP.waveActive
        ? CFG.WAVE_MS
        : 450
    );

}


/* ============================================================
   CONNECTION STATUS
============================================================ */

function setPill(
  id,
  active
) {

  const element =
    $(id);


  if (!element)
    return;


  element.classList.toggle(
    'active',
    active
  );

}


/* ============================================================
   CAMERA
============================================================ */

function initCamera() {

  const img =
    $('camera-feed');

  const placeholder =
    $('cam-placeholder');


  if (!img)
    return;


  img.onload = () => {

    APP.cameraOk =
      true;

    APP.cameraRetries =
      0;


    img.style.display =
      'block';


    if (placeholder) {

      placeholder.style.display =
        'none';
    }


    setPill(
      'pill-camera',
      true
    );


    startFPS();

  };


  img.onerror = () => {

    APP.cameraOk =
      false;


    if (placeholder) {

      placeholder.style.display =
        'flex';
    }


    setPill(
      'pill-camera',
      false
    );


    stopFPS();


    APP.cameraRetries++;


    if (
      APP.cameraRetries
      >
      CFG.CAMERA_MAX_RETRY
    ) {

      return;
    }


    const delay =
      Math.min(
        700 *
        Math.pow(
          1.55,
          APP.cameraRetries - 1
        ),
        10000
      );


    setTimeout(
      () => {

        img.src =
          `/video_feed?${Date.now()}`;

      },
      delay
    );

  };


  img.src =
    `/video_feed?${Date.now()}`;

}


/* ============================================================
   FPS
============================================================ */

function startFPS() {

  if (
    APP.fpsTimer
  ) {
    return;
  }


  APP.fpsTimer =
    setInterval(
      () => {

        const fps =
          APP.cameraOk
            ? 24 +
              Math.floor(
                Math.random() * 7
              )
            : 0;


        setText(
          'cam-fps',
          fps > 0
            ? fps
            : '--'
        );

      },
      CFG.FPS_MS
    );

}


function stopFPS() {

  if (
    APP.fpsTimer
  ) {

    clearInterval(
      APP.fpsTimer
    );

    APP.fpsTimer =
      null;
  }


  setText(
    'cam-fps',
    '--'
  );

}


/* ============================================================
   POLLING FALLBACK
============================================================ */

function initPolling() {

  setInterval(
    async () => {

      try {

        const response =
          await fetch(
            '/status',
            {
              cache:
                'no-store'
            }
          );


        const data =
          await response.json();


        applyState(
          data
        );

      } catch {

        // Socket.IO remains
        // the primary channel.
      }

    },
    CFG.POLL_MS
  );

}


/* ============================================================
   QUICK ACTIONS
============================================================ */

function initQuickActions() {

  const main =
    $('fab-main');

  const actions =
    document.querySelector(
      '.quick-actions'
    );

  const stop =
    $('fab-stop');

  const fullscreen =
    $('fab-fullscreen');


  if (
    !main
    ||
    !actions
  ) {
    return;
  }


  main.addEventListener(
    'click',
    event => {

      event.stopPropagation();

      APP.fabOpen =
        !APP.fabOpen;


      actions.classList.toggle(
        'open',
        APP.fabOpen
      );

    }
  );


  document.addEventListener(
    'click',
    event => {

      if (
        APP.fabOpen
        &&
        !event.target.closest(
          '.quick-actions'
        )
      ) {

        APP.fabOpen =
          false;

        actions.classList.remove(
          'open'
        );
      }

    }
  );


  if (stop) {

    stop.addEventListener(
      'click',
      () => {

        [
          'light1',
          'light2',
          'fan',
          'pump'
        ].forEach(
          device =>
            sendControl(
              device,
              'OFF'
            )
        );


        sendControl(
          'door',
          'LOCK'
        );


        log(
          'Emergency stop — all devices OFF',
          'danger',
          'octagon'
        );


        APP.fabOpen =
          false;

        actions.classList.remove(
          'open'
        );

      }
    );

  }


  if (fullscreen) {

    fullscreen.addEventListener(
      'click',
      async () => {

        try {

          if (
            !document.fullscreenElement
          ) {

            await document.documentElement
              .requestFullscreen();

          } else {

            await document.exitFullscreen();
          }

        } catch {

          // Fullscreen can be denied
          // by the browser.
        }

      }
    );

  }

}


/* ============================================================
   CAMERA RECONNECT
============================================================ */

function initCameraButton() {

  const button =
    $('btn-reconnect-cam');


  if (!button)
    return;


  button.addEventListener(
    'click',
    () => {

      const img =
        $('camera-feed');


      APP.cameraRetries =
        0;


      if (img) {

        img.src =
          `/video_feed?${Date.now()}`;
      }

    }
  );

}


/* ============================================================
   ICONS
============================================================ */

function initIcons() {

  if (
    window.lucide
  ) {

    lucide.createIcons();
  }

}


/* ============================================================
   FAN ANIMATION
============================================================ */

function injectFanAnimation() {

  const style =
    document.createElement(
      'style'
    );


  style.textContent = `

    @keyframes fanSpin {

      to {
        transform: rotate(360deg);
      }

    }

  `;


  document.head.appendChild(
    style
  );

}


/* ============================================================
   INIT
============================================================ */

async function init() {

  await runLoader();

  initIcons();

  initClock();

  injectFanAnimation();

  initSocket();

  initCamera();

  initCameraButton();

  initPolling();

  initQuickActions();

  initSOS();

  animateWave(
    false
  );


  log(
    'SmartHome AI initialized',
    'info',
    'check'
  );

}


document.addEventListener(
  'DOMContentLoaded',
  init
);