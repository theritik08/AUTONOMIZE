/* ============================================================
   Autonomize dashboard — behaviour
   Sections: theme · navigation · card tilt · reveal · charts
             · calendar · accordion · photo upload
   ============================================================ */

(function () {
  'use strict';

  /* Pure presentation logic — bucketing, chart geometry, tone selection,
     formatting. It lives in lib.js so it can be unit-tested directly in
     Node; keeping a second copy here is how a bucketing rule drifts out of
     step with the tests that claim to cover it. */
  var LIB = window.AutonomizeLib;

  var $  = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  var REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---- brand logo: falls back to a lettermark if assets/logo.png is absent ---- */
  var brandMark = $('#brandMark');
  brandMark.addEventListener('error', function () {
    brandMark.hidden = true;
    $('.brand').classList.add('no-logo');
  });

  /* ==========================================================
     Seed data — illustrative figures for the static demo
     ========================================================== */

  /* ==========================================================
     Live data — populated from the Autonomize backend, which is
     where the Chrome extension uploads every session.

     The shape below is EXACTLY the shape the previous hard-coded
     demo object had, so every renderer further down this file is
     unchanged. Only the source of the numbers moved.

       content-script.js  counts events, never text
       background.js      POST /api/session/upsert
       backend            scores it, stores it
       autonomize-api.js  GET /api/score + /api/sessions
       here               maps that into DATA and renders

     Empty rather than seeded: a dashboard that renders invented
     figures before the real ones arrive teaches people to distrust
     it the moment the two disagree.
     ========================================================== */

  var DATA = {
    score: null,
    rings: [
      { pct: 0, label: 'Independent', color: 'var(--green)' },
      { pct: 0, label: 'AI-assisted', color: 'var(--amber)' },
      { pct: 0, label: 'On track',    color: 'var(--amber)' }
    ],
    week: [
      { d: 'M', v: 0, c: 'var(--green)' }, { d: 'T', v: 0, c: 'var(--green)' },
      { d: 'W', v: 0, c: 'var(--green)' }, { d: 'T', v: 0, c: 'var(--green)' },
      { d: 'F', v: 0, c: 'var(--green)' }, { d: 'S', v: 0, c: 'var(--green)' },
      { d: 'S', v: 0, c: 'var(--green)' }
    ],
    chart: [],
    sessions: [],
    coins: null,
    graded: [],
    /* everything the renderers below did not previously need, kept
       beside them so nothing has to reach into a second global */
    raw: { score: null, sessions: [], settings: null, profile: null }
  };

  var MS_DAY = 86400000;

  /* ---- small shared formatters ------------------------------ */

  function agoLabel(ts) {
    if (!ts) return '';
    var days = Math.floor((Date.now() - ts) / MS_DAY);
    if (days <= 0) {
      var hours = Math.floor((Date.now() - ts) / 3600000);
      return hours <= 0 ? 'just now' : hours + 'h ago';
    }
    return days + 'd ago';
  }

  function dayKey(ts) {
    var d = new Date(ts);
    return d.getFullYear() + '-' +
           String(d.getMonth() + 1).padStart(2, '0') + '-' +
           String(d.getDate()).padStart(2, '0');
  }

  function shortDay(iso) {
    var parts = String(iso).split('-');
    var d = new Date(+parts[0], +parts[1] - 1, +parts[2]);
    return d.toLocaleDateString('en', { month: 'short', day: 'numeric' });
  }

  var CATEGORY_LABEL = {
    writing: 'Writing',
    assessment: 'Assessment',
    ai_assistant: 'AI assistant'
  };

  /* ---- API payload -> DATA ---------------------------------- */

  function mapAll(score, sessions) {
    DATA.raw.score = score;
    DATA.raw.sessions = sessions;

    DATA.score = score.current_score;

    /* rings: the same three quantities the React client shows, so the
       two surfaces cannot disagree about what "On track" means —
       it is the share of tracked days at or above your own baseline. */
    var indep = score.independent_minutes_7d || 0;
    var assisted = score.assisted_minutes_7d || 0;
    var total = indep + assisted;
    var trend = score.trend || [];
    var onTrack = (score.baseline_mean != null && trend.length)
      ? trend.filter(function (p) { return p.score >= score.baseline_mean; }).length / trend.length * 100
      : 0;

    DATA.rings = [
      { pct: total ? Math.round(indep / total * 100) : 0,    label: 'Independent', color: 'var(--green)' },
      { pct: total ? Math.round(assisted / total * 100) : 0, label: 'AI-assisted', color: 'var(--amber)' },
      { pct: Math.round(onTrack),                            label: 'On track',    color: 'var(--amber)' }
    ];

    /* weekly bars: tracked minutes per weekday, as a share of the busiest
       day. The bucketing lives in lib.js so it can be unit-tested without
       a browser; this only attaches the colour the bars render with. */
    DATA.week = LIB.weeklyBuckets(sessions, Date.now()).map(function (bucket) {
      return { d: bucket.d, v: bucket.v, minutes: bucket.minutes, c: 'var(--green)' };
    });

    /* composition chart: typed vs pasted characters per day */
    DATA.chart = (score.composition_trend || []).map(function (p) {
      return { day: shortDay(p.date), wrote: p.typed_chars || 0, pasted: p.pasted_chars || 0 };
    });

    /* recent activity list */
    DATA.sessions = sessions.slice(0, 7).map(function (s) {
      var tone = LIB.sessionTone(s);
      return {
        site: s.domain || 'unknown',
        cat: CATEGORY_LABEL[s.category] || s.category,
        ago: agoLabel(s.started_at),
        mins: Math.round((s.active_ms || 0) / 60000),
        score: s.score == null ? null : Math.round(s.score),
        tone: tone
      };
    });

    /* graded sessions (dark panel) */
    DATA.graded = (score.recent_assessment_sessions || []).map(function (g) {
      var bits = [];
      if (g.likely_ai_pastes) bits.push(g.likely_ai_pastes + ' AI-linked paste' + (g.likely_ai_pastes === 1 ? '' : 's'));
      if (g.tab_switch_count) bits.push(g.tab_switch_count + ' tab switches');
      return {
        site: g.domain || 'unknown',
        when: g.date,
        detail: bits.join(', ') || 'no flags',
        score: Math.round(g.score)
      };
    });

    DATA.coins = score.coins || null;
  }

  /* ==========================================================
     Theme
     ========================================================== */

  /* Three modes, not a toggle: Light, Dark, and System. The old header
     button flipped between two and remembered nothing, so a dark-mode user
     re-picked it on every load and someone whose OS switches at sunset was
     never followed. The control now lives in Profile -> Settings ->
     Preferences & Appearance, which is the single place configuration
     belongs; this module owns the behaviour and the persistence.

     Exposed on Autonomize.theme so the settings screen drives it rather
     than reimplementing it — two theme implementations is exactly the kind
     of duplication this refactor exists to remove. */

  var root = document.documentElement;
  var THEME_KEY = 'autonomize_theme';
  var media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

  function readThemeChoice() {
    try {
      var stored = localStorage.getItem(THEME_KEY);
      return stored === 'light' || stored === 'dark' || stored === 'system' ? stored : 'system';
    } catch (_) {
      return 'system';   /* private browsing */
    }
  }

  function applyTheme(choice) {
    var effective = choice === 'system'
      ? (media && media.matches ? 'dark' : 'light')
      : choice;
    root.setAttribute('data-theme', effective);
  }

  function setTheme(choice) {
    try { localStorage.setItem(THEME_KEY, choice); } catch (_) {}
    applyTheme(choice);
    document.querySelectorAll('[data-theme-choice]').forEach(function (btn) {
      var on = btn.getAttribute('data-theme-choice') === choice;
      btn.classList.toggle('is-on', on);
      btn.setAttribute('aria-checked', on ? 'true' : 'false');
    });
  }

  /* Applied before first paint below; re-applied when the OS flips, but
     ONLY while the user is on 'system' — an explicit choice must not be
     overridden by the operating system. */
  applyTheme(readThemeChoice());
  if (media && media.addEventListener) {
    media.addEventListener('change', function () {
      if (readThemeChoice() === 'system') applyTheme('system');
    });
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest && e.target.closest('[data-theme-choice]');
    if (btn) setTheme(btn.getAttribute('data-theme-choice'));
  });

  window.AutonomizeTheme = { get: readThemeChoice, set: setTheme, apply: applyTheme };

  /* ==========================================================
     Navigation
     ========================================================== */

  var navToggle = $('#navToggle');
  var nav = $('#primaryNav');

  navToggle.addEventListener('click', function () {
    var open = nav.classList.toggle('is-open');
    navToggle.setAttribute('aria-expanded', String(open));
    navToggle.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
  });

  $$('.nav-link').forEach(function (link) {
    link.addEventListener('click', function () {
      $$('.nav-link').forEach(function (l) {
        l.classList.remove('is-active');
        l.removeAttribute('aria-current');
      });
      link.classList.add('is-active');
      link.setAttribute('aria-current', 'page');
      nav.classList.remove('is-open');
      navToggle.setAttribute('aria-expanded', 'false');
    });
  });

  var topbar = $('.topbar');
  window.addEventListener('scroll', function () {
    topbar.classList.toggle('is-stuck', window.scrollY > 8);
  }, { passive: true });

  /* ==========================================================
     Card tilt — cursor-tracked 3D, capped so it stays subtle
     ========================================================== */

  var MAX_TILT = 5;

  if (!REDUCED && window.matchMedia('(hover: hover)').matches) {
    $$('.card-tilt').forEach(function (card) {
      card.addEventListener('mousemove', function (e) {
        var r = card.getBoundingClientRect();
        var px = (e.clientX - r.left) / r.width  - 0.5;
        var py = (e.clientY - r.top)  / r.height - 0.5;
        card.style.setProperty('--ry', (px *  MAX_TILT).toFixed(2) + 'deg');
        card.style.setProperty('--rx', (py * -MAX_TILT).toFixed(2) + 'deg');
      });
      card.addEventListener('mouseleave', function () {
        card.style.setProperty('--ry', '0deg');
        card.style.setProperty('--rx', '0deg');
      });
    });
  }

  /* ==========================================================
     Double-click to zoom a card
     The card is scaled and translated to the centre of the
     viewport, with the scale capped so it always fits on screen.
     Everything else dims; the zoomed card keeps full brightness.
     ========================================================== */

  (function zoom() {
    var ZOOM = 1.3;
    var MARGIN = 24;
    var REST = 'Double-click to zoom';
    var SHUT = 'Double-click to close';

    var open = null;
    var frame = null;

    function place(card) {
      /* measure the card in its untransformed position */
      card.style.removeProperty('transform');
      var r  = card.getBoundingClientRect();
      var vw = document.documentElement.clientWidth;
      var vh = document.documentElement.clientHeight;

      /* never let the card grow past the viewport */
      var scale = Math.min(ZOOM, (vw - MARGIN * 2) / r.width, (vh - MARGIN * 2) / r.height);
      scale = Math.max(scale, 1);

      var dx = vw / 2 - (r.left + r.width  / 2);
      var dy = vh / 2 - (r.top  + r.height / 2);

      card.style.setProperty(
        'transform',
        'translate(' + dx.toFixed(1) + 'px,' + dy.toFixed(1) + 'px) scale(' + scale.toFixed(3) + ')',
        'important'
      );
    }

    function reposition() {
      if (!open) return;
      if (frame) cancelAnimationFrame(frame);
      frame = requestAnimationFrame(function () { place(open); });
    }

    function close() {
      if (!open) return;
      open.style.removeProperty('transform');
      open.classList.remove('is-zoomed');
      open.querySelector('.zoom-hint').textContent = REST;
      var grid = open.closest('.grid');
      if (grid) grid.classList.remove('has-zoom');
      open = null;
      document.body.classList.remove('has-zoom');
    }

    $$('.card-tilt').forEach(function (card) {
      var hint = document.createElement('span');
      hint.className = 'zoom-hint';
      hint.textContent = REST;
      card.appendChild(hint);

      card.addEventListener('dblclick', function (e) {
        /* leave interactive controls alone */
        if (e.target.closest('button, input, a, .uploader, .seg')) return;

        if (open === card) { close(); return; }
        close();

        card.classList.add('is-zoomed');
        var grid = card.closest('.grid');
        if (grid) grid.classList.add('has-zoom');
        document.body.classList.add('has-zoom');
        hint.textContent = SHUT;
        open = card;
        place(card);
      });
    });

    /* clicking anywhere outside the zoomed card closes it */
    document.addEventListener('click', function (e) {
      if (open && !e.target.closest('.is-zoomed')) close();
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') close();
    });

    window.addEventListener('resize', reposition);
    window.addEventListener('scroll', reposition, { passive: true });
  })();

  /* ==========================================================
     Reveal on scroll
     ========================================================== */

  if (!REDUCED && 'IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry, i) {
        if (!entry.isIntersecting) return;
        var el = entry.target;
        setTimeout(function () { el.classList.add('is-in'); }, Math.min(i, 3) * 60);
        io.unobserve(el);
      });
    }, { rootMargin: '0px 0px -60px 0px', threshold: 0.06 });

    $$('.card').forEach(function (card) {
      card.classList.add('reveal');
      io.observe(card);
    });
  }

  /* ==========================================================
     Greeting
     ========================================================== */

  var h = new Date().getHours();
  $('#greeting').textContent =
    h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening';

  /* ==========================================================
     Independence gauge
     ========================================================== */

  var GAUGE_R = 90, GAUGE_LEN = Math.PI * GAUGE_R;

  function renderGauge() {
    var svg  = $('#gaugeSvg');
    var fill = $('#gaugeFill');
    var knob = $('#gaugeKnob');

    /* Built ONCE. This used to run on every render — and renderGauge runs
       on every poll and every SSE push — so the SVG accumulated a new
       <defs> with the same id="gaugeGrad" every few seconds. Unbounded
       DOM growth, and duplicate ids in a document where a later
       url(#gaugeGrad) reference is resolved against the first match. */
    if (!svg.querySelector('#gaugeGrad')) {
      var defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
      defs.innerHTML =
        '<linearGradient id="gaugeGrad" gradientUnits="userSpaceOnUse" x1="30" y1="0" x2="210" y2="0">' +
          '<stop offset="0%" stop-color="#D69A3B"/>' +
          '<stop offset="55%" stop-color="#8FAE55"/>' +
          '<stop offset="100%" stop-color="#4C9A6A"/>' +
        '</linearGradient>';
      svg.insertBefore(defs, svg.firstChild);
    }

    fill.style.strokeDasharray = GAUGE_LEN;
    fill.style.strokeDashoffset = GAUGE_LEN;

    /* No score yet is a real state, and it is NOT zero. The backend now
       returns null until there is something to report, so the gauge shows
       an empty arc and an em dash rather than a confident-looking 0 that
       a student would read as "you scored nothing". */
    var v = DATA.score;
    if (v == null || isNaN(v)) {
      svg.setAttribute('aria-label', 'Independence score not available yet');
      knob.setAttribute('cx', (120 + GAUGE_R * Math.cos(Math.PI)).toFixed(1));
      knob.setAttribute('cy', '140');
      var emptyLabel = $('#scoreValue');
      if (emptyLabel) emptyLabel.textContent = '—';
      return;
    }

    var theta = Math.PI * (1 - v / 100);
    svg.setAttribute('aria-label', 'Independence score ' + v + ' out of 100');

    requestAnimationFrame(function () {
      fill.style.strokeDashoffset = GAUGE_LEN * (1 - v / 100);
      knob.setAttribute('cx', (120 + GAUGE_R * Math.cos(theta)).toFixed(1));
      knob.setAttribute('cy', (140 - GAUGE_R * Math.sin(theta)).toFixed(1));
    });

    countTo($('#scoreValue'), v, 1200);
  }

  /* The frame handle of the count-up currently running, so a re-render
     can cancel it. Without this, every poll started ANOTHER rAF chain
     writing to the same element; two chains at different progress points
     fought over #scoreValue and the number visibly jittered between the
     old and new score instead of counting once to the new one. */
  var countFrame = null;

  function countTo(el, target, ms) {
    if (countFrame !== null) { cancelAnimationFrame(countFrame); countFrame = null; }
    if (REDUCED) { el.textContent = target; return; }
    var t0 = performance.now();
    (function step(now) {
      var p = Math.min((now - t0) / ms, 1);
      var eased = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.round(target * eased);
      countFrame = p < 1 ? requestAnimationFrame(step) : null;
    })(t0);
  }

  /* ==========================================================
     Hero rings
     ========================================================== */

  function renderRings() {
    var C = 2 * Math.PI * 24;
    $('#ringRow').innerHTML = DATA.rings.map(function (r) {
      return '<li class="ring-item">' +
        '<svg viewBox="0 0 58 58" role="img" aria-label="' + r.pct + ' per cent ' + r.label + '">' +
          '<circle class="ring-bg" cx="29" cy="29" r="24"/>' +
          '<circle class="ring-fg" cx="29" cy="29" r="24" stroke="' + r.color + '" ' +
            'stroke-dasharray="' + C.toFixed(1) + '" stroke-dashoffset="' + C.toFixed(1) + '" ' +
            'transform="rotate(-90 29 29)"/>' +
          '<text class="ring-pct" x="29" y="33" text-anchor="middle" ' +
            'style="color:' + r.color + '">' + r.pct + '%</text>' +
        '</svg>' +
        '<span class="ring-label">' + r.label + '</span>' +
      '</li>';
    }).join('');

    requestAnimationFrame(function () {
      $$('#ringRow .ring-fg').forEach(function (c, i) {
        c.style.strokeDashoffset = C * (1 - DATA.rings[i].pct / 100);
      });
    });
  }

  /* ==========================================================
     Weekly bars
     ========================================================== */

  function renderWeekBars() {
    $('#weekBars').innerHTML = DATA.week.map(function (d) {
      return '<li class="week-bar" title="' + d.d + ' — ' + d.v + '% of your busiest day">' +
               '<span style="height:0;background:' + d.c + '"></span>' +
             '</li>';
    }).join('');

    requestAnimationFrame(function () {
      $$('#weekBars span').forEach(function (s, i) {
        s.style.height = DATA.week[i].v + '%';
      });
    });
  }

  /* ==========================================================
     Composition chart — bar view and two-line view
     ========================================================== */

  var renderChart = null;

  (function chart() {
    var host   = $('#chart');
    var legend = $('#chartLegend');
    /* The card this chart lives in, used to scope its segmented control
       away from the identically-classed controls elsewhere on the page. */
    var chartCard = host ? host.closest('.card, section, .view') : null;
    var rows   = [];
    var peak = 0, linePeak = 0;
    var currentView = 'bars';

    var axis = document.createElement('p');
    axis.className = 'chart-axis';
    host.insertAdjacentElement('afterend', axis);

    /* ---- legend ---- */
    function drawLegend(view) {
      var wrote  = rows.reduce(function (t, d) { return t + d.wrote; }, 0);
      var pasted = rows.reduce(function (t, d) { return t + d.pasted; }, 0);
      var total  = wrote + pasted;
      var yours  = total ? Math.round(wrote / total * 100) : 0;
      legend.innerHTML = view === 'bars'
        ? '<span class="legend-item"><i class="swatch swatch-green"></i>You wrote <strong>' + kfmt(wrote) + '</strong></span>' +
          '<span class="legend-item"><i class="swatch swatch-amber"></i>Pasted in <strong>' + kfmt(pasted) + '</strong></span>' +
          '<span class="legend-pct">' + yours + '% yours</span>'
        : '<span class="legend-item"><i class="swatch swatch-line swatch-manual"></i>Written manually <strong>' + kfmt(wrote) + '</strong></span>' +
          '<span class="legend-item"><i class="swatch swatch-line swatch-ai"></i>AI-copied / pasted <strong>' + kfmt(pasted) + '</strong></span>' +
          '<span class="legend-pct">' + yours + '% yours</span>';
    }

    /* ---- bar view ---- */
    function drawBars() {
      host.innerHTML = rows.map(function (d) {
        return '<div class="chart-col" title="' + d.day + '">' +
          '<span class="chart-tip">' + d.day + ' · ' + d.wrote + ' written / ' + d.pasted + ' pasted</span>' +
          '<div class="bar-pasted" style="height:0" data-h="' + (d.pasted / peak * 100).toFixed(2) + '"></div>' +
          '<div class="bar-wrote"  style="height:0" data-h="' + (d.wrote  / peak * 100).toFixed(2) + '"></div>' +
        '</div>';
      }).join('');
      host.style.display = 'grid';

      requestAnimationFrame(function () {
        $$('#chart [data-h]').forEach(function (bar) {
          bar.style.height = bar.getAttribute('data-h') + '%';
        });
      });
    }

    /* ---- line view: smooth curves over a soft filled area ---- */
    var W = 760, H = 300, PL = 46, PR = 14, PT = 16, PB = 44;
    var PLOT_W = W - PL - PR, PLOT_H = H - PT - PB;

    var niceMax = LIB.niceMax;
    var TOP_V = 1;

    /* A single day of data is the NORMAL state for a new account, not an
       edge case — and `rows.length - 1` is zero there, so every x came out
       NaN and the SVG refused the path. One point is drawn at the left
       edge, which is where a series of length one belongs. */
    function xStep() { return rows.length > 1 ? PLOT_W / (rows.length - 1) : 0; }
    function xAt(i) { return PL + i * xStep(); }
    function yAt(v) { return PT + PLOT_H - (v / TOP_V) * PLOT_H; }

    var kfmt = LIB.kfmt;

    /* Catmull-Rom through the points, converted to cubic beziers */
    function curve(key) {
      var p = rows.map(function (d, i) { return [xAt(i), yAt(d[key])]; });
      var out = 'M' + p[0][0].toFixed(1) + ',' + p[0][1].toFixed(1);

      /* A path that is only a moveto draws NOTHING. With one day of data
         — the normal state for a new account, not an edge case — the loop
         below never runs, so the previous version produced an invisible
         line and the chart looked broken on exactly the day a new user
         first opens it.

         A short horizontal segment is the honest mark for a single
         sample: it shows the value without implying a trend that one
         point cannot support. */
      if (p.length === 1) {
        return out + 'L' + (p[0][0] + 28).toFixed(1) + ',' + p[0][1].toFixed(1);
      }

      for (var i = 0; i < p.length - 1; i++) {
        var p0 = p[i - 1] || p[i], p1 = p[i], p2 = p[i + 1], p3 = p[i + 2] || p2;
        out += 'C' + (p1[0] + (p2[0] - p0[0]) / 6).toFixed(1) + ',' + (p1[1] + (p2[1] - p0[1]) / 6).toFixed(1) +
               ' ' + (p2[0] - (p3[0] - p1[0]) / 6).toFixed(1) + ',' + (p2[1] - (p3[1] - p1[1]) / 6).toFixed(1) +
               ' ' + p2[0].toFixed(1) + ',' + p2[1].toFixed(1);
      }
      return out;
    }

    function area(key) {
      return curve(key) + 'L' + xAt(rows.length - 1).toFixed(1) + ',' + (PT + PLOT_H) +
             'L' + xAt(0).toFixed(1) + ',' + (PT + PLOT_H) + 'Z';
    }

    function dots(key, cls) {
      return rows.map(function (d, i) {
        return '<circle class="line-dot ' + cls + '" data-i="' + i + '" cx="' + xAt(i).toFixed(1) +
               '" cy="' + yAt(d[key]).toFixed(1) + '" r="0"/>';
      }).join('');
    }

    /* hover readout: nearest data point, crosshair, enlarged dots */
    function bindReadout() {
      var svg    = host.querySelector('.line-chart');
      var tip    = host.querySelector('.line-tip');
      var cursor = host.querySelector('.line-cursor');
      /* Guarded for the same single-row reason as xAt(); a zero step also
         makes the hover index calculation below degenerate, so it is
         clamped rather than divided by. */
      var step   = xStep();
      var hot    = -1;

      function clearHot() {
        $$('.line-dot.is-hot', host).forEach(function (c) { c.classList.remove('is-hot'); });
      }

      function hide() {
        hot = -1; clearHot();
        tip.hidden = true;
        cursor.classList.remove('is-on');
      }

      function move(e) {
        var box = svg.getBoundingClientRect();
        var x   = (e.clientX - box.left) / box.width * W;
        // step is 0 when there is only one row; that division would be
        // Infinity/NaN and the clamp below would not save it.
        var i   = step > 0 ? Math.round((x - PL) / step) : 0;
        i = Math.max(0, Math.min(rows.length - 1, i));
        if (i === hot) return;
        hot = i;

        var d = rows[i];
        clearHot();
        $$('.line-dot[data-i="' + i + '"]', host).forEach(function (c) { c.classList.add('is-hot'); });

        cursor.setAttribute('x1', xAt(i).toFixed(1));
        cursor.setAttribute('x2', xAt(i).toFixed(1));
        cursor.classList.add('is-on');

        tip.innerHTML =
          '<p class="line-tip-day">' + d.day + '</p>' +
          '<p class="line-tip-row"><span><i class="line-tip-dot t-manual"></i>Typed</span>' +
            '<strong>' + d.wrote.toLocaleString() + '</strong></p>' +
          '<p class="line-tip-row"><span><i class="line-tip-dot t-ai"></i>Pasted</span>' +
            '<strong>' + d.pasted.toLocaleString() + '</strong></p>';
        tip.hidden = false;

        var left = xAt(i) / W * box.width;
        tip.style.left = Math.max(86, Math.min(box.width - 86, left)) + 'px';
        tip.style.top  = (Math.min(yAt(d.wrote), yAt(d.pasted)) / H * box.height) + 'px';
      }

      svg.addEventListener('mousemove', move);
      svg.addEventListener('mouseleave', hide);
    }

    function drawLines() {
      host.style.display = 'block';

      var ticks = [0, 0.25, 0.5, 0.75, 1];
      var gridY = ticks.map(function (t) {
        var y = (PT + PLOT_H - t * PLOT_H).toFixed(1);
        return '<line class="line-grid" x1="' + PL + '" y1="' + y + '" x2="' + (W - PR) + '" y2="' + y + '"/>' +
               '<text class="line-axis" x="' + (PL - 10) + '" y="' + (+y + 4) + '" text-anchor="end">' +
                 kfmt(Math.round(TOP_V * t)) + '</text>';
      }).join('');

      var labels = rows.map(function (d, i) {
        if (i % 2 !== 0 && i !== rows.length - 1) return '';
        return '<text class="line-axis" x="' + xAt(i).toFixed(1) + '" y="' + (H - 16) +
               '" text-anchor="middle">' + d.day + '</text>';
      }).join('');

      host.innerHTML =
        '<svg class="line-chart" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" ' +
             'role="img" aria-label="Characters typed versus pasted, each day">' +
          '<defs>' +
            '<linearGradient id="fillManual" x1="0" y1="0" x2="0" y2="1">' +
              '<stop offset="0%" stop-color="#4C9A6A" stop-opacity=".26"/>' +
              '<stop offset="100%" stop-color="#4C9A6A" stop-opacity="0"/>' +
            '</linearGradient>' +
            '<linearGradient id="fillAi" x1="0" y1="0" x2="0" y2="1">' +
              '<stop offset="0%" stop-color="#CF5C48" stop-opacity=".22"/>' +
              '<stop offset="100%" stop-color="#CF5C48" stop-opacity="0"/>' +
            '</linearGradient>' +
          '</defs>' +
          gridY + labels +
          '<line class="line-cursor" y1="' + PT + '" y2="' + (PT + PLOT_H) + '" x1="0" x2="0"/>' +
          '<path class="line-area" d="' + area('wrote')  + '" fill="url(#fillManual)"/>' +
          '<path class="line-area" d="' + area('pasted') + '" fill="url(#fillAi)"/>' +
          '<path class="line-path line-ai"     d="' + curve('pasted') + '"/>' +
          '<path class="line-path line-manual" d="' + curve('wrote')  + '"/>' +
          dots('pasted', 'd-ai') +
          dots('wrote',  'd-manual') +
        '</svg>' +
        '<div class="line-tip" hidden></div>';

      $$('#chart .line-path').forEach(function (path) {
        var len = path.getTotalLength();
        path.style.strokeDasharray = len;
        path.style.strokeDashoffset = REDUCED ? 0 : len;
        if (!REDUCED) requestAnimationFrame(function () { path.style.strokeDashoffset = 0; });
      });
      requestAnimationFrame(function () {
        $$('#chart .line-dot').forEach(function (c) { c.setAttribute('r', '3.6'); });
      });

      bindReadout();
    }

    function render(view) {
      currentView = view || currentView;
      rows = DATA.chart;
      if (!rows.length) {
        host.innerHTML = '';
        legend.innerHTML = '';
        axis.innerHTML = '';
        return;
      }
      peak     = rows.reduce(function (m, d) { return Math.max(m, d.wrote + d.pasted); }, 0) || 1;
      linePeak = rows.reduce(function (m, d) { return Math.max(m, d.wrote, d.pasted); }, 0) || 1;
      TOP_V    = niceMax(linePeak);
      axis.innerHTML = '<span>' + rows[0].day + '</span>' +
                       '<span>' + rows[rows.length - 1].day + '</span>';
      drawLegend(currentView);
      if (currentView === 'bars') drawBars(); else drawLines();
    }

    /* Exposed so fresh data redraws the chart without rebinding the
       segmented control below — binding it twice would fire two
       renders per click and animate the bars from the wrong start. */
    renderChart = render;

    /* Scoped to THIS chart's own buttons.
       `.seg-btn` is used in three unrelated places — the chart's
       Bars/Lines toggle, the Sessions category filter, and the Theme
       radios. An unscoped selector meant clicking a Sessions filter or a
       Theme button cleared `is-on` from the chart's toggle, marked an
       unrelated button as the active chart view, corrupted `aria-pressed`
       on the theme radiogroup, and fired a redundant chart render with
       `data-view` of null. */
    var chartSegs = chartCard
      ? Array.prototype.slice.call(chartCard.querySelectorAll('.seg-btn[data-view]'))
      : $$('.seg-btn[data-view]');

    chartSegs.forEach(function (btn) {
      btn.addEventListener('click', function () {
        chartSegs.forEach(function (b) {
          b.classList.remove('is-on');
          b.setAttribute('aria-pressed', 'false');
        });
        btn.classList.add('is-on');
        btn.setAttribute('aria-pressed', 'true');
        render(btn.getAttribute('data-view'));
      });
    });

  })();

  /* ==========================================================
     Autonomize Coins
     Earned for finishing work unaided; spent when text is pasted in.
     ========================================================== */

  function renderCoins() {
    var C = DATA.coins;
    if (!C) return;

    /* Balance, tier and weekly movement are computed by the backend
       (backend/coins.py) rather than here. The rule printed on this
       card — +10 for a clean session, -1 per 100 pasted characters —
       is applied in exactly one place, so the extension popup, this
       page and any future client cannot arrive at different balances
       from the same sessions. */
    var balance = C.balance;
    var week    = C.week_delta;

    $('#coinTier').textContent = C.tier;
    $('#coinNext').textContent = C.next_tier
      ? C.to_next + ' to ' + C.next_tier
      : 'top tier';

    requestAnimationFrame(function () {
      $('#coinFill').style.width = Math.round(C.tier_progress * 100) + '%';
    });

    var delta = $('#coinDelta');
    delta.textContent = (week >= 0 ? '+' : '\u2212') + Math.abs(week) + ' this week';
    delta.className = 'coin-delta ' + (week >= 0 ? 'up' : 'down');

    $('#coinLedger').innerHTML = C.ledger.slice().reverse().map(function (e) {
      var up = e.delta >= 0;
      var why = e.pasted === 0
        ? 'nothing pasted'
        : e.pasted.toLocaleString() + ' characters pasted';
      /* The card's "task" line wants a document name. The extension
         never captures one — no titles, no text, by design — so the
         most specific honest label is the session's category and
         date. `task_names_available: false` in the payload says so
         explicitly rather than leaving this looking broken. */
      var task = (e.category === 'assessment' ? 'Graded session' : 'Writing session') +
                 ' \u00b7 ' + shortDay(dayKey(e.started_at));
      return '<li class="coin-row">' +
        '<span class="coin-row-body">' +
          '<span class="coin-task">' + task + '</span>' +
          '<span class="coin-why">' + e.site + ' \u00b7 ' + why + '</span>' +
        '</span>' +
        '<span class="coin-amt ' + (up ? 'up' : 'down') + '">' +
          (up ? '+' : '\u2212') + Math.abs(e.delta) +
        '</span>' +
      '</li>';
    }).join('');

    countTo($('#coinBalance'), balance, 1200);
  }

  /* ==========================================================
     Session list
     ========================================================== */

  /* renderSessionList() was deleted, not disabled. app.js owns the
     Sessions view, including its category filter and its limited-tracking
     labelling; keeping a second, filter-unaware implementation here was
     the duplication that emptied the page. */


  /* ==========================================================
     Graded sessions (dark panel)
     ========================================================== */

  function renderGraded() {
    $('#gradedList').innerHTML = DATA.graded.map(function (g) {
      return '<li class="graded-item">' +
        '<svg class="graded-flag" viewBox="0 0 24 24" aria-hidden="true">' +
          '<path d="M5 21V4"/><path d="M5 5h11l-2 3 2 3H5"/></svg>' +
        '<span class="graded-body">' +
          '<span class="graded-site">' + g.site + '</span>' +
          '<span class="graded-meta">' + g.when + ' · <em>' + g.detail + '</em></span>' +
        '</span>' +
        '<span class="graded-score">' + g.score + '</span>' +
      '</li>';
    }).join('');
  }

  /* ==========================================================
     Activity calendar — click a day for its typed/pasted detail
     ========================================================== */

  var renderCalendar = null;

  (function calendar() {
    var MONTHS = ['January','February','March','April','May','June',
                  'July','August','September','October','November','December'];

    var grid    = $('#calGrid');
    var detail  = $('#calDetail');
    var label   = $('#calMonth');
    var today   = new Date();
    var view    = new Date(today.getFullYear(), today.getMonth(), 1);
    var picked  = new Date(today.getFullYear(), today.getMonth(), today.getDate());

    /* Real sessions, indexed by local calendar day.
       This replaced a deterministic pseudo-random generator that
       invented plausible days. The generator was the right call for a
       static demo and is exactly wrong once real data exists: it would
       keep drawing convincing activity for days the student did not
       work, and nobody could tell the difference by looking. */
    var CATEGORY_CLASS = {
      'AI assistant': 'c-ai',
      'Assessment': 'c-assessment',
      'Writing': ''
    };

    var byDay = {};

    function indexSessions() {
      byDay = {};
      (DATA.raw.sessions || []).forEach(function (s) {
        if (!s.started_at) return;
        var key = dayKey(s.started_at);
        (byDay[key] = byDay[key] || []).push(s);
      });
      Object.keys(byDay).forEach(function (key) {
        byDay[key].sort(function (a, b) { return a.started_at - b.started_at; });
      });
    }

    function sessionsFor(date) {
      var list = byDay[dayKey(date.getTime())] || [];
      return list.map(function (s) {
        var cat = CATEGORY_LABEL[s.category] || s.category;
        var when = new Date(s.started_at);
        return {
          host: s.domain || 'unknown',
          cat: cat,
          cls: CATEGORY_CLASS[cat] || '',
          typed: s.typed_chars || 0,
          pasted: s.pasted_chars || 0,
          mins: Math.round((s.active_ms || 0) / 60000),
          start: String(when.getHours()).padStart(2, '0') + ':' +
                 String(when.getMinutes()).padStart(2, '0')
        };
      });
    }

    function totals(list) {
      return list.reduce(function (t, s) {
        t.typed += s.typed; t.pasted += s.pasted; t.mins += s.mins;
        t.flagged = t.flagged || (s.cat === 'Assessment' && s.pasted > 0);
        return t;
      }, { typed: 0, pasted: 0, mins: 0, flagged: false });
    }

    var level = LIB.heatLevel;

    var same = LIB.sameDay;

    function drawGrid() {
      var y = view.getFullYear(), m = view.getMonth();
      label.textContent = MONTHS[m] + ' ' + y;

      var first = new Date(y, m, 1).getDay();
      var days  = new Date(y, m + 1, 0).getDate();
      var html  = '';

      for (var p = 0; p < first; p++) html += '<span class="cal-day is-pad"></span>';

      for (var d = 1; d <= days; d++) {
        var date = new Date(y, m, d);
        var t    = totals(sessionsFor(date));
        var lv   = level(t.typed + t.pasted);
        var cls  = 'cal-day';
        if (same(date, picked)) cls += ' is-on';
        if (same(date, today))  cls += ' is-today';
        if (t.flagged)          cls += ' is-flagged';

        html += '<button type="button" class="' + cls + '" data-d="' + d + '" ' +
                'aria-label="' + MONTHS[m] + ' ' + d + ', ' +
                (t.typed + t.pasted) + ' characters">' +
                  d + '<i class="cal-dot k' + lv + '"></i>' +
                '</button>';
      }
      grid.innerHTML = html;
    }

    function drawDetail() {
      var list = sessionsFor(picked);
      var t    = totals(list);
      var all  = t.typed + t.pasted;
      var share = all ? Math.round(t.typed / all * 100) : 0;

      var head =
        '<div class="cal-detail-head">' +
          '<p class="cal-date">' + MONTHS[picked.getMonth()] + ' ' + picked.getDate() + '</p>' +
          '<p class="cal-share">' + (all ? share + '% yours \u00b7 ' + t.mins + 'm tracked' : 'nothing tracked') + '</p>' +
        '</div>';

      if (!list.length) {
        detail.innerHTML = head +
          '<p class="cal-empty">No activity recorded on this day. Days with tracked work show a ' +
          'coloured dot in the calendar.</p>';
        return;
      }

      var stats =
        '<div class="cal-totals">' +
          '<div class="cal-stat typed"><b>' + t.typed.toLocaleString() + '</b><span>characters typed</span></div>' +
          '<div class="cal-stat pasted"><b>' + t.pasted.toLocaleString() + '</b><span>characters pasted</span></div>' +
          '<div class="cal-stat"><b>' + list.length + '</b><span>' + (list.length === 1 ? 'session' : 'sessions') + '</span></div>' +
        '</div>' +
        '<div class="cal-split">' +
          '<i class="s-typed" style="flex-basis:' + share + '%"></i>' +
          '<i class="s-pasted" style="flex-basis:' + (100 - share) + '%"></i>' +
        '</div>';

      var rows = '<ul class="cal-sessions">' + list.map(function (s) {
        var tot = s.typed + s.pasted;
        var pct = tot ? Math.round(s.typed / tot * 100) : 0;
        return '<li class="cal-session">' +
          '<div class="cal-session-top">' +
            '<span class="cal-site">' + s.host + '</span>' +
            '<span class="cal-cat ' + s.cls + '">' + s.cat + '</span>' +
            '<span class="cal-when">' + s.start + ' \u00b7 ' + s.mins + 'm</span>' +
          '</div>' +
          '<div class="cal-session-nums">' +
            '<span class="n-typed">typed <b>' + s.typed.toLocaleString() + '</b></span>' +
            '<span class="n-pasted">pasted <b>' + s.pasted.toLocaleString() + '</b></span>' +
          '</div>' +
          '<div class="cal-session-bar">' +
            '<i class="s-typed" style="flex-basis:' + pct + '%;background:#4C9A6A"></i>' +
            '<i class="s-pasted" style="flex-basis:' + (100 - pct) + '%;background:#CF5C48"></i>' +
          '</div>' +
        '</li>';
      }).join('') + '</ul>';

      detail.innerHTML = head + stats + rows;
    }

    function render() { indexSessions(); drawGrid(); drawDetail(); }

    /* Exposed for the same reason the chart's is: fresh data must
       redraw without rebinding the month arrows and day grid. */
    renderCalendar = render;

    grid.addEventListener('click', function (e) {
      var btn = e.target.closest('.cal-day');
      if (!btn || btn.classList.contains('is-pad')) return;
      picked = new Date(view.getFullYear(), view.getMonth(), +btn.getAttribute('data-d'));
      render();
    });

    $('#calPrev').addEventListener('click', function () {
      view = new Date(view.getFullYear(), view.getMonth() - 1, 1);
      drawGrid();
    });
    $('#calNext').addEventListener('click', function () {
      view = new Date(view.getFullYear(), view.getMonth() + 1, 1);
      drawGrid();
    });

  })();

  /* ==========================================================
     Accordion
     ========================================================== */

  $$('.acc-head').forEach(function (head) {
    head.addEventListener('click', function () {
      var item = head.parentElement;
      var open = item.classList.toggle('is-open');
      head.setAttribute('aria-expanded', String(open));
    });
  });

  /* ==========================================================
     Values written straight into index.html

     These were literals in the markup — the chips, the score delta,
     the weekly total, the Today ring, the exam panel, the accordion
     statuses and the two "Connected" pills. They are reached through
     the selectors the markup already has, so index.html needs no new
     ids, no new elements and no structural change.
     ========================================================== */

  function text(sel, value) {
    var el = $(sel);
    if (el) el.textContent = value;
  }

  var fmtMinutes = LIB.fmtMinutes;

  function renderStatic() {
    var d = DATA.raw.score;
    var sessions = DATA.raw.sessions || [];
    if (!d) return;

    /* ---- hero chips: sessions / streak / sites (last 7 days) ---- */
    var week = sessions.filter(function (s) {
      return s.started_at && Date.now() - s.started_at < 7 * MS_DAY;
    });
    var sites = {};
    week.forEach(function (s) { if (s.domain) sites[s.domain] = 1; });

    var chips = $$('.chip-row .chip strong');
    if (chips[0]) chips[0].textContent = week.length;
    if (chips[1]) chips[1].textContent = d.streak_days != null ? d.streak_days : 0;
    if (chips[2]) chips[2].textContent = Object.keys(sites).length;

    /* ---- score card delta and note ---- */
    var delta = d.delta_vs_baseline;
    if (delta != null && d.baseline_mean != null) {
      text('#scoreDelta', (delta >= 0 ? '+' : '\u2212') + Math.abs(Math.round(delta * 10) / 10) +
                          ' vs. your baseline (' + Math.round(d.baseline_mean) + ')');
    } else {
      text('#scoreDelta', 'No baseline yet \u2014 still learning how you work');
    }
    var note = $('.card-score .card-note');
    if (note) {
      note.textContent = delta == null
        ? 'A few more sessions and this will compare you to your own baseline.'
        : delta <= -8 ? 'Below your usual baseline right now \u2014 recent sessions leaned more AI-assisted.'
        : delta >= 8  ? 'Above your own baseline \u2014 more of this work was yours than usual.'
        : 'Steady, right around your own baseline.';
    }

    /* ---- weekly total ---- */
    var weeklyMs = week.reduce(function (t, s) { return t + (s.active_ms || 0); }, 0);
    var weekly = fmtMinutes(weeklyMs / 60000);
    var totalSpans = $$('.weekly-total span');
    if (totalSpans[0]) totalSpans[0].textContent = weekly.h;
    if (totalSpans[1]) totalSpans[1].textContent = weekly.m;

    /* ---- Today ---- */
    var todayKey = dayKey(Date.now());
    var todays = sessions.filter(function (s) {
      return s.started_at && dayKey(s.started_at) === todayKey;
    });
    var indepMs = 0, assistedMs = 0;
    todays.forEach(function (s) {
      if (s.category === 'ai_assistant') assistedMs += s.active_ms || 0;
      else indepMs += s.active_ms || 0;
    });
    var todayTotal = indepMs + assistedMs;
    var share = todayTotal ? Math.round(indepMs / todayTotal * 100) : null;
    /* An em dash, not 0%. "Nothing tracked yet today" and "none of
       today's work was yours" are different claims, and only one of
       them is true before the first session. */
    text('.today-dash', share == null ? '\u2014' : share + '%');
    text('.today-total', Math.round(todayTotal / 60000) + 'm');

    /* ---- chart footer: the real forecast ---- */
    var f = d.forecast;
    var foot = $('.chart-foot .legend-item');
    var footPct = $('.chart-foot .legend-pct');
    if (foot) {
      if (!f || f.projected_score == null) {
        foot.innerHTML = '<i class="swatch swatch-green"></i>' +
          'Not enough of a pattern yet to project a trend.';
      } else {
        var dir = f.slope_per_day >= 0 ? 'up' : 'down';
        foot.innerHTML = '<i class="swatch swatch-green"></i>Trending ' + dir +
          ' about ' + Math.abs(f.slope_per_day).toFixed(1) + ' points a day \u2014 on track for roughly ' +
          Math.round(f.projected_score) + ' in ' + f.horizon_days + ' days.';
      }
    }
    if (footPct) footPct.textContent = f ? 'fit ' + Math.round(f.r2 * 100) + '%' : 'fit \u2014';

    /* ---- exam & assignment integrity ---- */
    var risk = d.assessment_risk_level;
    var badge = $('.badge-risk');
    if (badge) {
      badge.innerHTML = '<span class="status-dot dot-risk" aria-hidden="true"></span>' +
        (risk ? risk.charAt(0).toUpperCase() + risk.slice(1) + ' risk' : 'No graded work yet');
    }
    var riskScore = $('.risk-score');
    if (riskScore) {
      riskScore.innerHTML = (d.assessment_score == null ? '\u2014' : Math.round(d.assessment_score)) +
                            '<small>/100</small>';
    }
    var riskNote = $('.risk-line') && $('.risk-line').parentElement.querySelector('.card-note');
    if (riskNote) {
      riskNote.textContent = d.assessment_delta == null
        ? 'No graded sessions recorded yet.'
        : (d.assessment_delta >= 0 ? '+' : '\u2212') +
          Math.abs(Math.round(d.assessment_delta * 10) / 10) + ' vs. your exam baseline';
    }
    var meter = $('.meter-fill');
    if (meter) meter.style.width = (d.assessment_score == null ? 0 : Math.round(d.assessment_score)) + '%';

    /* ---- accordion statuses ---- */
    var statuses = $$('.acc-item .acc-status');
    var bodies   = $$('.acc-item .acc-body p');
    if (statuses[0]) statuses[0].textContent = connected ? 'Connected' : 'Disconnected';
    if (bodies[0]) {
      bodies[0].innerHTML = 'Scores are computed by your backend at <code>' +
        Autonomize.config.backendUrl + '</code>. The dashboard polls every 30 seconds and ' +
        'keeps showing the last known data if the connection drops.';
    }
    var settings = DATA.raw.settings;
    if (statuses[1] && settings) {
      var on = Object.keys(settings.tracking).filter(function (k) { return settings.tracking[k]; });
      statuses[1].textContent = on.length + ' of 3 enabled';
    }
    if (statuses[2]) {
      var busiest = null, byDomain = {};
      week.forEach(function (s) {
        if (!s.domain) return;
        byDomain[s.domain] = (byDomain[s.domain] || 0) + (s.active_ms || 0);
      });
      Object.keys(byDomain).forEach(function (dom) {
        if (!busiest || byDomain[dom] > byDomain[busiest]) busiest = dom;
      });
      statuses[2].textContent = busiest || 'none yet';
    }

    /* ---- avatar initials from the signed-in profile ---- */
    var profile = DATA.raw.profile;
    if (profile) {
      var name = profile.display_name || profile.email || '';
      var initials = name.replace(/[^A-Za-z ]/g, ' ').trim().split(/\s+/)
        .slice(0, 2).map(function (w) { return w.charAt(0).toUpperCase(); }).join('');
      if (initials) text('#navInitials', initials);
    }
  }

  /* ---- the two "Connected" pills ---- */
  var connected = false;

  function renderConnection(online) {
    connected = online;
    var label = online ? 'Connected' : 'Disconnected';
    var pill = $('.status-pill');
    if (pill) {
      pill.innerHTML = '<span class="status-dot" aria-hidden="true"' +
        (online ? '' : ' style="background:var(--risk)"') + '></span>' + label;
    }
    var foot = $('.foot-status');
    if (foot) {
      foot.innerHTML = label + ' <span class="status-dot" aria-hidden="true"' +
        (online ? '' : ' style="background:var(--risk)"') + '></span>';
    }
  }

  /* ==========================================================
     Photo upload — the header avatar is the drop target.
     FileReader only; the image never leaves this browser.
     ========================================================== */

  (function uploader() {
    var MAX_BYTES = 2 * 1024 * 1024;
    var TYPES = ['image/jpeg', 'image/png', 'image/webp'];

    /* The photo control moved into Profile -> Settings. The header avatar
       is now the profile-menu trigger, and one control cannot both open a
       menu and open a file picker. Every lookup is guarded so this module
       degrades quietly if the markup changes again rather than throwing
       and taking the rest of the renderer down with it. */
    var wrap     = $('.set-avatar-row');
    var avatar   = $('#setAvatar');
    var trigger  = $('#photoBtn');
    var initials = $('#navInitials');
    var input    = $('#fileInput');
    var msg      = $('#uploadError');
    var timer    = null;

    if (!wrap || !avatar || !trigger || !input || !msg) return;

    function flash(text) {
      msg.textContent = text;
      msg.hidden = false;
      clearTimeout(timer);
      timer = setTimeout(function () { msg.hidden = true; }, 3600);
    }

    function show(src) {
      var img = avatar.querySelector('img') || new Image();
      img.src = src;
      img.alt = '';
      if (!img.parentNode) avatar.appendChild(img);
      if (initials) initials.hidden = true;
      avatar.setAttribute('aria-label', 'Profile photo set.');
    }

    function load(file) {
      if (!file) return;
      if (TYPES.indexOf(file.type) === -1) return flash('Choose a JPG, PNG or WebP image.');
      if (file.size > MAX_BYTES)           return flash('That image is over 2 MB.');

      msg.hidden = true;
      var reader = new FileReader();
      reader.onload  = function (e) { show(e.target.result); };
      reader.onerror = function () { flash('That file could not be read.'); };
      reader.readAsDataURL(file);
    }

    trigger.addEventListener('click', function () { input.click(); });

    input.addEventListener('change', function () {
      load(input.files[0]);
      input.value = '';
    });

    ['dragenter', 'dragover'].forEach(function (n) {
      wrap.addEventListener(n, function (e) { e.preventDefault(); wrap.classList.add('is-over'); });
    });
    ['dragleave', 'dragend', 'drop'].forEach(function (n) {
      wrap.addEventListener(n, function (e) { e.preventDefault(); wrap.classList.remove('is-over'); });
    });
    wrap.addEventListener('drop', function (e) { load(e.dataTransfer.files[0]); });
  })();


  /* ==========================================================
     Boot — connect, load, render, poll

     Order matters. `Autonomize.connect()` establishes identity before
     anything is fetched: the dashboard does not choose who it is, it
     presents a token it already holds or asks the backend for a device
     identity and is told. Rendering before that resolves would fire
     unauthenticated reads that 401.
     ========================================================== */

  function renderAll() {
    renderGauge();
    renderRings();
    renderWeekBars();
    if (renderChart) renderChart();
    renderCoins();
    /* #sessionList is painted by app.js, which owns the Sessions view and
       its category filter. Calling renderSessionList() here as well meant
       two renderers writing one element, and whichever ran last won —
       which is why the Sessions page kept coming up empty. */
    renderGraded();
    if (renderCalendar) renderCalendar();
    renderStatic();
  }

  function applyPayload(score, sessions) {
    mapAll(score, sessions);
    renderConnection(true);
    renderAll();
  }

  async function refresh() {
    try {
      var score = await Autonomize.score();
      var list  = await Autonomize.sessions(400);
      applyPayload(score, list.sessions || []);
      return true;
    } catch (error) {
      /* The last good data stays on screen behind a Disconnected pill.
         Blanking every card because one poll failed turns a momentary
         blip into "all your data is gone", which is a far worse lie
         than a number that is thirty seconds old. */
      renderConnection(false);
      console.warn('[autonomize]', error.message);
      return false;
    }
  }

  /* ==========================================================
     Renderer entry point

     This module no longer boots itself, connects, or polls. app.js owns
     identity and the fetch loop and calls in here with the payload it
     already has.

     The reason is not tidiness. Two modules each calling connect() and
     each running a 30-second timer meant two independent identity
     resolutions and four requests per cycle against the same two
     endpoints — and, worse, this one would happily render a dashboard for
     an anonymous device account that app.js had correctly refused to let
     past the auth gate. One fetch, one source of truth, one gate.
     ========================================================== */

  window.AutonomizeRender = function (score, sessions) {
    applyPayload(score, sessions || []);
  };

  /* Settings arrive from app.js, which already reads them for the
     Tracking screen — this only keeps the renderer's copy in step. */
  window.AutonomizeRenderSettings = function (settings) {
    DATA.raw.settings = settings;
  };

  /* The old "Settings" header button and the accordion it scrolled to are
     both gone. Settings now lives at #/settings, reached from the profile
     avatar — one entry point, one implementation. Nothing replaces this
     block; it is deleted rather than rewired because the duplication was
     the problem. */

})();
