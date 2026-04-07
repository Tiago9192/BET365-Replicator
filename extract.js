(function(){
  var APP = window._bet365AppUrl || location.origin;

  // If already injected, just run extraction
  if (document.getElementById('b365-extractor-btn')) {
    extract();
    return;
  }

  // Create floating button
  var btn = document.createElement('div');
  btn.id = 'b365-extractor-btn';
  btn.innerHTML = '📋 Enviar a mi app';
  btn.style.cssText = [
    'position:fixed',
    'bottom:80px',
    'right:16px',
    'z-index:99999',
    'background:#00ff88',
    'color:#000',
    'font-family:Arial,sans-serif',
    'font-size:14px',
    'font-weight:bold',
    'padding:12px 18px',
    'border-radius:12px',
    'cursor:pointer',
    'box-shadow:0 4px 20px rgba(0,255,136,0.5)',
    'user-select:none',
    'border:none',
    'outline:none'
  ].join(';');

  btn.onclick = function() {
    btn.innerHTML = '⏳ Extrayendo...';
    btn.style.background = '#ffcc00';
    extract();
  };

  document.body.appendChild(btn);

  // Auto-extract after 2 seconds
  setTimeout(extract, 2000);

  function extract() {
    var runners = [];
    var url = window.location.href;
    var fiMatch    = url.match(/\/F(\d+)\//);
    var sportMatch = url.match(/\/B(\d+)\//);
    var fi    = fiMatch    ? fiMatch[1]    : '0';
    var sport = sportMatch ? sportMatch[1] : '73';

    document.querySelectorAll('.rh-af').forEach(function(row, i) {
      var nameEl = row.querySelector('span.rh-2c');
      var oddEls = row.querySelectorAll('span.rul-ce0412');
      var oddEl  = oddEls[0];
      if (nameEl && oddEl) {
        var name = nameEl.innerText.trim();
        var odd  = oddEl.innerText.trim();
        if (name && odd && parseFloat(odd) > 1.0 && name.length > 1) {
          runners.push({
            name:     name,
            odd:      odd,
            fi:       fi,
            sport_id: parseInt(sport),
            index:    i
          });
        }
      }
    });

    if (runners.length === 0) {
      // Retry up to 5 times
      if (!window._b365Retries) window._b365Retries = 0;
      window._b365Retries++;
      if (window._b365Retries < 5) {
        if (btn) btn.innerHTML = '⏳ Cargando (' + window._b365Retries + '/5)...';
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
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({
        runners:  runners,
        url:      url,
        fi:       parseInt(fi),
        sport_id: parseInt(sport)
      })
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.success) {
        if (btn) {
          btn.innerHTML = '✓ ' + d.count + ' caballos enviados!';
          btn.style.background = '#00ff88';
          setTimeout(function(){
            btn.innerHTML = '📋 Enviar a mi app';
          }, 3000);
        }
      } else {
        if (btn) btn.innerHTML = '❌ Error: ' + (d.error || '?');
      }
    })
    .catch(function(e) {
      if (btn) btn.innerHTML = '❌ Error conexión';
      console.error('Bet365 extractor error:', e);
    });
  }
})();
