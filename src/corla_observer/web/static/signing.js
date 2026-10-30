// corla-observer: signing observer entries on the device (docs/RECEIPTS.md).
//
// The canonical serialization is that of docs/EVENT_LOG.md (sorted keys, no
// whitespace, integers only, UTF-8), mirrored from
// corla_observer.events.canonical_json; the payloads are those that
// corla_observer.receipts.check_payload accepts. tests/test_receipts.py runs
// this file in Node and checks its payloads and signatures with the Python
// code. A device is an ECDSA P-256 key pair made with Web Crypto, its private
// key non-extractable; each payload carries the device's next sequence
// number and the hash of its previous payload. Loads in a browser
// (window.CorlaSigning) or in Node.
"use strict";

(function (root) {
  var ZERO_HASH = "0000000000000000000000000000000000000000000000000000000000000000";
  var ALGORITHM = "ECDSA-P256-SHA256";
  var subtle = root.crypto && root.crypto.subtle;

  function canonical(value) {
    if (value === null || typeof value === "boolean") return JSON.stringify(value);
    if (typeof value === "number") {
      if (!Number.isSafeInteger(value)) throw new Error("only integers can be signed: " + value);
      return String(value);
    }
    if (typeof value === "string") return JSON.stringify(value);
    if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
    if (typeof value === "object") {
      return "{" + Object.keys(value).sort().map(function (key) {
        return JSON.stringify(key) + ":" + canonical(value[key]);
      }).join(",") + "}";
    }
    throw new Error("not a JSON value");
  }

  // Text as a payload may carry it: no control characters, trimmed, at most
  // `max` characters (counted as code points, as Python counts them).
  function cleanText(text, max) {
    var cleaned = String(text).replace(/[\u0000-\u001f\u007f-\u009f]/g, "").trim();
    var chars = Array.from(cleaned);
    return chars.length > max ? chars.slice(0, max).join("").trim() : cleaned;
  }

  function bytes(text) { return new TextEncoder().encode(text); }

  function hex(buffer) {
    return Array.prototype.map.call(new Uint8Array(buffer), function (b) { return ("0" + b.toString(16)).slice(-2); }).join("");
  }

  function base64(buffer) {
    var view = new Uint8Array(buffer);
    var text = "";
    for (var i = 0; i < view.length; i++) text += String.fromCharCode(view[i]);
    return root.btoa(text);
  }

  function sha256Hex(text) {
    return subtle.digest("SHA-256", bytes(text)).then(hex);
  }

  function available() { return Boolean(subtle); }

  // A signed item: the payload, its hash and the device's signature over it.
  function sign(device, payload) {
    var text = canonical(payload);
    return Promise.all([
      subtle.sign({ name: "ECDSA", hash: "SHA-256" }, device.keys.privateKey, bytes(text)),
      sha256Hex(text)
    ]).then(function (results) {
      return { device_seq: payload.device_seq, payload: payload, signature: base64(results[0]), payload_sha256: results[1] };
    });
  }

  // A new device for an observer link, and its signed registration (sequence
  // number 0). `link` is {observer, county, board}; `clientTime` an ISO time.
  function newDevice(link, clientTime) {
    return subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign", "verify"]).then(function (keys) {
      return subtle.exportKey("raw", keys.publicKey).then(function (raw) {
        return subtle.digest("SHA-256", raw).then(function (digest) {
          var device = {
            keys: keys,
            device_id: "d-" + hex(digest).slice(0, 32),
            public_key: base64(raw),
            observer: link.observer,
            county: link.county,
            board: String(link.board),
            seq: 0,
            prev: ZERO_HASH
          };
          var payload = {
            v: 1,
            type: "device_registration",
            observer: device.observer,
            device_id: device.device_id,
            county: device.county,
            board: device.board,
            public_key: device.public_key,
            algorithm: ALGORITHM,
            client_time: clientTime,
            device_seq: 0,
            prev_payload_sha256: ZERO_HASH
          };
          return sign(device, payload).then(function (signed) {
            device.prev = signed.payload_sha256;
            return { device: device, registration: signed };
          });
        });
      });
    });
  }

  // The device's next signed entry. `fields` has type ("entry",
  // "first_reading" or "heard_id"), extract_sha256, position, imprinted_id, content and
  // client_time. Advances the device's sequence number and chain.
  function signNext(device, fields) {
    var payload = {
      v: 1,
      type: fields.type,
      observer: device.observer,
      device_id: device.device_id,
      county: device.county,
      board: device.board,
      extract_sha256: fields.extract_sha256,
      position: fields.position,
      imprinted_id: fields.imprinted_id,
      content: fields.content,
      client_time: fields.client_time,
      device_seq: device.seq + 1,
      prev_payload_sha256: device.prev
    };
    return sign(device, payload).then(function (signed) {
      device.seq = payload.device_seq;
      device.prev = signed.payload_sha256;
      return signed;
    });
  }

  var api = {
    ZERO_HASH: ZERO_HASH,
    ALGORITHM: ALGORITHM,
    available: available,
    canonical: canonical,
    cleanText: cleanText,
    sha256Hex: sha256Hex,
    newDevice: newDevice,
    signNext: signNext
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.CorlaSigning = api;
})(typeof window !== "undefined" ? window : globalThis);
