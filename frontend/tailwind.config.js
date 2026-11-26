/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      keyframes: {
        "tick-pulse": {
          "0%": { opacity: "1", transform: "scale(1)" },
          "50%": { opacity: "0.35", transform: "scale(1.6)" },
          "100%": { opacity: "1", transform: "scale(1)" },
        },
        "fade-in-up": {
          "0%": { opacity: "0", transform: "translateY(6px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        "tick-pulse": "tick-pulse 0.55s ease-out",
        "fade-in-up": "fade-in-up 0.25s ease-out both",
      },
    },
  },
  plugins: [],
};
