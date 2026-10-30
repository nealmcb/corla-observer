// corla-observer: the photo check's form. When a photo chosen in either of
// the form's file inputs is larger than the check needs (its longer side
// over the form's data-shrink-side pixels, or its size over
// data-shrink-bytes), it is redrawn smaller in the browser, upright by its
// EXIF orientation, and sent as a JPEG instead of the original. The form
// works without this script: the server reduces a large photo itself, up
// to its upload limit.
"use strict";

(function () {
  var form = document.querySelector("form[data-shrink-side]");
  if (!form || !window.createImageBitmap || !window.DataTransfer || !HTMLCanvasElement.prototype.toBlob) return;
  var maxSide = parseInt(form.getAttribute("data-shrink-side"), 10) || 4000;
  var maxBytes = parseInt(form.getAttribute("data-shrink-bytes"), 10) || 8 * 1024 * 1024;
  var status = document.getElementById("photo-shrink");
  var button = form.querySelector("button[type=submit]");
  var pending = 0;

  function mb(n) {
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }

  function say(text) {
    if (!status) return;
    status.textContent = text;
    status.hidden = !text;
  }

  function busy(delta) {
    pending += delta;
    if (button) button.disabled = pending > 0;
  }

  function toBlob(canvas) {
    return new Promise(function (resolve) {
      canvas.toBlob(resolve, "image/jpeg", 0.92);
    });
  }

  function shrink(input) {
    var file = input.files && input.files[0];
    if (!file || !/^image\//.test(file.type || "image/")) {
      say("");
      return;
    }
    busy(1);
    say("Chosen: " + mb(file.size) + ". Preparing…");
    createImageBitmap(file, { imageOrientation: "from-image" })
      .then(function (bitmap) {
        var w = bitmap.width;
        var h = bitmap.height;
        var longest = Math.max(w, h);
        if (longest <= maxSide && file.size <= maxBytes) {
          if (bitmap.close) bitmap.close();
          say("Chosen: " + mb(file.size) + " (" + w + "×" + h + "); sent as it is.");
          return;
        }
        var scale = Math.min(1, maxSide / longest);
        var canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(w * scale));
        canvas.height = Math.max(1, Math.round(h * scale));
        var context = canvas.getContext("2d");
        context.imageSmoothingEnabled = true;
        context.imageSmoothingQuality = "high";
        context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
        if (bitmap.close) bitmap.close();
        return toBlob(canvas).then(function (blob) {
          if (!blob || blob.size >= file.size) {
            say("Chosen: " + mb(file.size) + " (" + w + "×" + h + "); sent as it is.");
            return;
          }
          var name = (file.name || "photo").replace(/\.[^.]*$/, "") + ".jpg";
          var transfer = new DataTransfer();
          transfer.items.add(new File([blob], name, { type: "image/jpeg", lastModified: file.lastModified }));
          input.files = transfer.files;
          say("Chosen: " + mb(file.size) + " (" + w + "×" + h + "); sending " + mb(blob.size) + " (" + canvas.width + "×" + canvas.height + ").");
        });
      })
      .catch(function () {
        // Not decodable here (HEIC on some browsers, or too large for the device): send it as chosen.
        say("Chosen: " + mb(file.size) + "; sent as it is.");
      })
      .then(function () {
        busy(-1);
      });
  }

  var inputs = form.querySelectorAll("input[type=file]");
  for (var i = 0; i < inputs.length; i++) {
    inputs[i].addEventListener("change", function (event) {
      // One photo is checked at a time: choosing one clears the other input.
      for (var j = 0; j < inputs.length; j++) {
        if (inputs[j] !== event.target && event.target.files && event.target.files.length) inputs[j].value = "";
      }
      shrink(event.target);
    });
  }
  form.addEventListener("submit", function (event) {
    if (pending > 0) event.preventDefault();
  });
})();
