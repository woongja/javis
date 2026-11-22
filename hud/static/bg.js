"use strict";

(() => {
  const canvas = document.getElementById("backdrop");
  let context;
  try {
    context = canvas.getContext("2d");
  } catch {
    return;
  }
  if (!context) return;

  function draw() {
    const { width, height } = canvas.getBoundingClientRect();
    if (!width || !height) return;
    const density = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * density);
    canvas.height = Math.round(height * density);
    context.setTransform(density, 0, 0, density, 0, 0);

    const centerX = width * .82;
    const centerY = height * .51;
    const radius = Math.min(height * .34, width * .23);
    const tilt = .38;
    const turn = -.25;

    function spherePoint(latitude, longitude) {
      const x = Math.cos(latitude) * Math.cos(longitude);
      const y = Math.sin(latitude);
      const z = Math.cos(latitude) * Math.sin(longitude);
      const pitchY = y * Math.cos(tilt) - z * Math.sin(tilt);
      const depth = y * Math.sin(tilt) + z * Math.cos(tilt);
      const scale = radius * 4 / (4 - depth);
      return [
        centerX + scale * (x * Math.cos(turn) - pitchY * Math.sin(turn)),
        centerY + scale * (x * Math.sin(turn) + pitchY * Math.cos(turn)),
      ];
    }

    function trace(pointAt, color) {
      context.beginPath();
      context.moveTo(...pointAt(0));
      for (let step = 1; step <= 48; step++) context.lineTo(...pointAt(step / 48));
      context.strokeStyle = color;
      context.stroke();
    }

    context.fillStyle = "rgba(133, 239, 216, .48)";
    for (let dot = 0; dot < 56; dot++) {
      const x = width * (.51 + ((dot * 67) % 113) / 240);
      const y = height * (.08 + ((dot * 43) % 107) / 126);
      context.fillRect(x, y, dot % 7 === 0 ? 2 : 1, 1);
    }

    for (let i = -4; i <= 4; i++) {
      trace((t) => spherePoint(i * Math.PI / 12, t * Math.PI * 2), "rgba(117, 237, 208, .37)");
    }
    for (let i = 0; i < 12; i++) {
      trace((t) => spherePoint((t - .5) * Math.PI, i * Math.PI / 6), "rgba(117, 237, 208, .36)");
    }
    context.strokeStyle = "rgba(117, 237, 208, .52)";
    context.beginPath();
    context.ellipse(centerX, centerY, radius * 1.55, radius * .37, -.43, 0, 2 * Math.PI);
    context.stroke();
  }

  draw();
  if (typeof ResizeObserver === "function") {
    new ResizeObserver(draw).observe(canvas);
  } else {
    window.addEventListener("resize", draw);
  }
})();
