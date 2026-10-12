/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        background: "#F6F7FB",
        surface: {
          DEFAULT: "#FFFFFF",
          secondary: "#FAF9FD",
          hover: "#F8F6FF",
        },
        primary: {
          DEFAULT: "#7C5CFC",
          hover: "#6E4EF2",
          soft: "#F0EBFF",
        },
        border: {
          DEFAULT: "#E9E7EF",
          hover: "#D9D2F5",
        },
        text: {
          primary: "#20232D",
          secondary: "#707582",
          tertiary: "#A0A5B1",
        },
        success: "#16A36A",
        warning: "#D99A20",
        danger: "#E05260",
        info: "#3B82F6",
      },
      borderRadius: {
        shell: "28px",
        panel: "22px",
        card: "18px",
        control: "14px",
        small: "10px",
        pill: "999px",
      },
      boxShadow: {
        card: "0 1px 2px rgba(30,24,45,0.02), 0 8px 30px rgba(45,30,80,0.06)",
        "card-hover":
          "0 2px 4px rgba(30,24,45,0.03), 0 12px 36px rgba(45,30,80,0.09)",
      },
      fontFamily: {
        sans: [
          "Inter",
          "Geist",
          "SF Pro",
          "PingFang SC",
          "Microsoft YaHei",
          "Noto Sans SC",
          "sans-serif",
        ],
      },
    },
  },
  plugins: [],
};
