// Saves the app on the device so it runs without Wi-Fi.
// When you upload changed files, bump VERSION (and APP_VERSION in index.html).
// Devices notice the new VERSION and switch over on their own.
var VERSION = 'choreboard-v4';
var FILES = ['./', './index.html', './manifest.json', './icon.png'];

self.addEventListener('install', function (e) {
  e.waitUntil(
    caches.open(VERSION).then(function (c) {
      // Fetch fresh copies, not whatever the browser cached earlier
      return Promise.all(FILES.map(function (f) {
        return fetch(f + '?v=' + Date.now()).then(function (res) {
          if (!res.ok) throw new Error('fetch failed: ' + f);
          return c.put(f, res);
        });
      }));
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k !== VERSION; })
        .map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

function isPage(req) {
  if (req.mode === 'navigate') return true;
  var path = new URL(req.url).pathname;
  return /\/$/.test(path) || /\/index\.html$/.test(path);
}

// The page itself: try the internet first so updates show up, fall back to the saved copy
function pageFirst(req) {
  var clean = req.url.split('?')[0];
  return caches.open(VERSION).then(function (cache) {
    function saved() {
      return cache.match(clean).then(function (r) { return r || cache.match('./index.html'); });
    }
    return new Promise(function (resolve) {
      var settled = false;
      function finish(r) { if (!settled && r) { settled = true; resolve(r); } }
      var timer = setTimeout(function () { saved().then(finish); }, 4000);   // slow Wi-Fi: use the saved copy
      fetch(clean + '?fresh=' + Date.now(), { credentials: 'same-origin' }).then(function (res) {
        if (res && res.ok && !res.redirected) {
          cache.put(clean, res.clone());
          clearTimeout(timer);
          finish(res);
        } else {
          saved().then(function (r) { finish(r || res); });
        }
      }).catch(function () {
        clearTimeout(timer);
        saved().then(function (r) { finish(r || Response.error()); });
      });
    });
  });
}

// Everything else (icon, manifest): saved copy first, refreshed quietly
function savedFirst(req) {
  return caches.open(VERSION).then(function (cache) {
    return cache.match(req, { ignoreSearch: true }).then(function (cached) {
      var network = fetch(req).then(function (res) {
        if (res && res.ok && res.type === 'basic') cache.put(req, res.clone());
        return res;
      }).catch(function () { return cached; });
      return cached || network;
    });
  });
}

self.addEventListener('fetch', function (e) {
  if (e.request.method !== 'GET') return;
  if (e.request.url.indexOf(self.registration.scope) !== 0) return;   // only our own files
  e.respondWith(isPage(e.request) ? pageFirst(e.request) : savedFirst(e.request));
});
