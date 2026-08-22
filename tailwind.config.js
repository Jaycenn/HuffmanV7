/** Tailwind build config for the AFC dashboard.
 *  Produces static/css/tailwind.css so the app needs NO CDN and works offline.
 *  Rebuild after editing templates:  sh tools/build_css.sh                   */
module.exports = {
  darkMode: 'class',
  content: ["./templates/**/*.html", "./static/js/**/*.js"],
  theme: { extend: {
    colors: { paper:"#F5F6F2", ink:"#17201D", dim:"#65706C", line:"#DCE2DC",
              maroon:"#146C5E", maroondark:"#0E554A", gold:"#9A6614" },
    fontFamily: { display:["Georgia","Times New Roman","serif"],
                  data:["ui-monospace","Consolas","monospace"] } } },
};
