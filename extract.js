(function(){
  var APP = window._bet365AppUrl || location.origin;

  if (document.getElementById('b365-extractor-btn')) {
    extract();
    return;
  }

  var btn = document.createElement('div');
  btn.id = 'b365-extractor-btn';
  btn.innerHTML = '📋 Enviar a mi app';
  btn.style.cssText = [
    'position:fixed','bottom:80px','right:16px','z-index:99999',
    'background:#00ff88','color:#000','font-family:Arial,sans-serif',
    'font-size:14px','font-weight:bold','padding:12px 18px',
    'border-radius:12px','cursor:pointer',
    'box-shadow:0 4px 20px rgba(0,255,136,0.5)',
    'user-select:none','border:none','outline:none'
  ].join(';');

  btn.onclick = function() {
    btn.innerHTML = '⏳ Extrayendo...';
    btn.style.background = '#ffcc00';
    extract();
  };

  document.body.appendChild(btn);
  setTimeout(extract, 2000);

  function getRaceName() {
    var venue = '';
    var raceNum = '';

    // Try to get venue name from header
    var allEls = document.querySelectorAll('*');
    for (var i = 0; i < allEls.length; i++) {
      var el = allEls[i];
      if (el.childElementCount === 0 && el.innerText) {
        var t = el.innerText.trim();
        // Race number pattern: "Race 1", "Race 2", etc
        if (/^race\s+\d+$/i.test(t) && !raceNum) {
          raceNum = t;
        }
      }
    }

    // Venue from header elements
    var headerEls = document.querySelectorAll('[class*="rcl-Header"],[class*="MarketHeader"],[class*="rh-b"],[class*="rh-c"]');
    for (var j = 0; j < headerEls.length; j++) {
      var ht = headerEls[j].innerText.trim().split('\n')[0].trim();
      if (ht && ht.length > 2 && ht.length < 40 && !/^\d+$/.test(ht)) {
        venue = ht;
        break;
      }
    }

    // Fallback: page title
    if (!venue && document.title) {
      venue = document.title.replace('bet365','').replace('|','').trim();
    }

    if (venue && raceNum) return venue + ' — ' + raceNum;
    if (venue) return venue;
    if (raceNum) return raceNum;
    return 'Carrera';
  }

  function extract() {
    var runners = [];
    var url = window.location.href;
    var fiMatch    = url.match(/\/F(\d+)\//);
    var sportMatch = url.match(/\/B(\d+)\//);
    var fi    = fiMatch    ? fiMatch[1]    : '0';
    var sport = sportMatch ? sportMatch[1] : '73';
    var raceName = getRaceName();

    // Extract runners — include SP ones too
    document.querySelectorAll('.rh-af').forEach(function(row, i) {
      var nameEl = row.querySelector('span.rh-2c');
      var oddEls = row.querySelectorAll('span.rul-ce0412');
      var oddEl  = oddEls[0];
      if (nameEl) {
        var name = nameEl.innerText.trim();
        var odd  = oddEl ? oddEl.innerText.trim() : 'SP';
        if (name && name.length > 1) {
          runners.push({
            name: name, odd: odd || 'SP',
            fi: fi, sport_id: parseInt(sport), index: i
          });
        }
      }
    });

    if (runners.length === 0) {
      if (!window._b365Retries) window._b365Retries = 0;
      window._b365Retries++;
      if (window._b365Retries < 6) {
        if (btn) btn.innerHTML = '⏳ Buscando caballos (' + window._b365Retries + '/6)...';
        setTimeout(extract, 1500);
        return;
      }
      if (btn) {
        btn.innerHTML = '❌ Sin caballos — toca de nuevo';
        btn.style.background = '#ff4466';
        btn.style.color = '#fff';
        setTimeout(function(){
          btn.innerHTML = '📋 Enviar a mi app';
          btn.style.background = '#00ff88';
          btn.style.color = '#000';
          window._b365Retries = 0;
        }, 3000);
      }
      return;
    }

    window._b365Retries = 0;

    fetch(APP + '/api/race/from-browser', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        runners: runners, url: url,
        fi: parseInt(fi), sport_id: parseInt(sport),
        race_name: raceName
      })
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.success) {
        var action = d.action === 'updated' ? 'Cuotas actualizadas!' : d.count + ' caballos cargados!';
        if (btn) {
          btn.innerHTML = '✓ ' + action;
          setTimeout(function(){ btn.innerHTML = '📋 Enviar a mi app'; }, 3000);
        }
      } else {
        if (btn) btn.innerHTML = '❌ ' + (d.error || 'Error');
      }
    })
    .catch(function(e) {
      if (btn) btn.innerHTML = '❌ Error conexión';
      console.error('Extractor error:', e);
    });
  }
})();
