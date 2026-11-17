/* Move-car alerts for the installed iPhone app. Rules are always fetched from the network. */
self.addEventListener("install", function (event) {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", function (event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", function (event) {
  var data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = {title: "Move your car", body: event.data ? event.data.text() : "Time to move the car."};
  }
  event.waitUntil(self.registration.showNotification(data.title || "Move your car", {
    body: data.body || "Time to move the car.",
    tag: data.tag || "move-car",
    renotify: true,
    data: {url: data.url || "/"}
  }));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(self.clients.matchAll({type: "window", includeUncontrolled: true}).then(function (list) {
    for (var i = 0; i < list.length; i++) {
      if (list[i].url.indexOf(self.location.origin) === 0 && "focus" in list[i]) {
        var focused = list[i].focus();
        if ("navigate" in list[i]) return list[i].navigate(url);
        return focused;
      }
    }
    return self.clients.openWindow(url);
  }));
});
