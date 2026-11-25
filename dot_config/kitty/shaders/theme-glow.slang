#language slang 2026
// Dezentes Hintergrundglühen in der Akzentfarbe des Themes.
//
// Zwei weiche Lichtflecken wandern langsam über das Fenster und hellen nur
// Pixel auf, die (fast) die Hintergrundfarbe haben – Text, Tab-Leiste und
// Bilder bleiben unverändert. Die Farbe kommt aus der Cursorfarbe, folgt
// also automatisch dem matugen-Theme (dank-theme.conf).
//
// Die Parameter lassen sich in theme-glow.pipeline per `var` überschreiben.

import kitty_custom_shader_types;

static const float GLOW_INTENSITY = 0.02;  // Stärke des Glühens (lineares RGB)
static const float GLOW_SPEED = 0.05;      // Wandergeschwindigkeit
static const float GLOW_RADIUS = 0.55;     // Radius der Lichtflecken (Anteil der Fensterhöhe)
static const float BG_TOLERANCE = 0.02;    // bis zu diesem Farbabstand gilt ein Pixel als Hintergrund

float glow_blob(float2 p, float2 centre) {
    float2 delta = p - centre;
    return exp(-dot(delta, delta) / (GLOW_RADIUS * GLOW_RADIUS));
}

float glow_dither(float2 p) {
    return frac(sin(dot(p, float2(12.9898, 78.233))) * 43758.5453) - 0.5;
}

public float4
fragment_main(float4 color, KittyTextures t, KittyCustomShaderData d) {
    // Nur Hintergrundpixel einfärben
    float bg_distance = length(color.rgb - d.background.rgb);
    float mask = 1.0 - smoothstep(0.0, BG_TOLERANCE, bg_distance);
    if (mask <= 0.0) return color;

    // Akzentfarbe: Cursorfarbe, ersatzweise die Vordergrundfarbe
    float has_cursor = step(0.0001, dot(d.cursor_color.rgb, float3(1.0)));
    float3 accent = lerp(d.foreground.rgb, d.cursor_color.rgb, has_cursor);

    float2 pixel_dims = float2(d.viewport_size_pixels) * t.viewport.zw;
    float2 aspect = float2(pixel_dims.x / pixel_dims.y, 1.0);
    float2 p = t.pos * aspect;

    float time = d.timestamp * GLOW_SPEED;
    float2 c1 = float2(0.5 + 0.45 * sin(time), 0.5 + 0.40 * cos(time * 0.7)) * aspect;
    float2 c2 = float2(0.5 + 0.45 * sin(time * 0.6 + 2.0), 0.5 + 0.40 * sin(time * 0.9 + 4.0)) * aspect;
    float glow = min(glow_blob(p, c1) + 0.7 * glow_blob(p, c2), 1.2);

    // Leichtes Rauschen gegen Farbstufen im dunklen Verlauf
    float dither = glow_dither(t.pos * pixel_dims) * 0.0008 * glow;

    float3 result = color.rgb + mask * (accent * GLOW_INTENSITY * glow + dither);
    return float4(max(result, float3(0.0)), color.a);
}
