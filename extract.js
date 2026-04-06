(function(){
  // Bet365 horse racing data extractor
  // Selectors found via DOM inspection: span.rh-2c = horse name, span.rul-ce0412[0] = WIN odd
  var APP = window._bet365AppUrl || location.origin;

  function extract() {
    var runners = [];
    var url = window.location.href;

    // Extract race params from URL
    var fiMatch    = url.match(/\/F(\d+)\//);
    var sportMatch = url.match(/\/B(\d+)\//);
    var fi         = fiMatch    ? fiMatch[1]    : "0";
    var sport      = sportMatch ? sportMatch[1] : "73";

    // Find all horse rows
    var rows = document.querySelectorAll(".rh-af");

    rows.forEach(function(row, i) {
      // Horse name: span.rh-2c
      var nameEl = row.querySelector("span.rh-2c");

      // WIN odd: first span.rul-ce0412 (Ganador column)
      var oddEls = row.querySelectorAll("span.rul-ce0412");
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

    return runners;
  }

  function send(runners) {
    var url = window.location.href;
    var fiMatch    = url.match(/\/F(\d+)\//);
    var sportMatch = url.match(/\/B(\d+)\//);

    fetch(APP + "/api/race/from-browser", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({
        runners:  runners,
        url:      url,
        fi:       fiMatch    ? parseInt(fiMatch[1])    : 0,
        sport_id: sportMatch ? parseInt(sportMatch[1]) : 73
      })
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.success) {
        alert("\u2713 " + d.count + " caballos cargados en tu app!\nVuelve a tu app y selecciona el ganador.");
      } else {
        alert("Error: " + (d.error || "desconocido"));
      }
    })
    .catch(function(e) {
      alert("Error conectando con la app: " + e.message);
    });
  }

  // Try immediately, retry if page not loaded yet
  var attempts = 0;
  function tryExtract() {
    attempts++;
    var runners = extract();
    if (runners.length > 0) {
      send(runners);
    } else if (attempts < 10) {
      setTimeout(tryExtract, 800);
    } else {
      alert("No se encontraron caballos.\nAsegurate de:\n1. Estar en la pagina de una carrera\n2. Que las cuotas esten visibles (sin candado)\n3. Estar en la tab 'Racecard' o 'Ganador'");
    }
  }

  tryExtract();
})();
