/** Tailwind build config for the AFC dashboard.
 *  Produces static/css/tailwind.css so the app needs NO CDN and works offline.
 *  Rebuild after editing templates:  sh tools/build_css.sh                   */
module.exports = {
  darkMode: 'class',
  content: ["./templates/**/*.html", "./static/js/**/*.js"],
  theme: { extend: {
    colors: { paper:"#FBFAF7", ink:"#21201C", dim:"#6B675E", line:"#E4E0D6",
              maroon:"#8E1F1F", maroondark:"#701717", gold:"#B57A17" },
    fontFamily: { display:["Georgia","Times New Roman","serif"],
                  data:["ui-monospace","Consolas","monospace"] } } },
};
