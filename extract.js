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
  setTimeout(extract, 3000);

  function getRaceName() {
    // Exact selectors found via DOM inspection
    var venueEl  = document.querySelector('.rcr-a4');  // e.g. "Philadelphia"
    var raceEl   = document.querySelector('.rcr-b');   // e.g. "Race 7"

    var venue   = venueEl  ? venueEl.innerText.trim()  : '';
    var raceNum = raceEl   ? raceEl.innerText.trim()   : '';

    // Try alternative selectors if main ones not found
    if (!venue) {
      var alt = document.querySelector('[class*="rcr-a"]');
      if (alt) venue = alt.innerText.trim();
    }
    if (!raceNum) {
      var alt2 = document.querySelector('[class*="rcr-b"]');
      if (alt2) raceNum = alt2.innerText.trim();
    }

    if (venue && raceNum) return venue + ' — ' + raceNum;
    if (venue)   return venue;
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
