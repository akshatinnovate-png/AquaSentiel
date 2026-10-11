/* Overridden at deploy time.
 *
 * The CDK stack writes this file into the dashboard bucket with its API
 * endpoint, which makes the page talk to a real backend. Locally it stays
 * null, and the dashboard falls back to whatever it can find: a backend on
 * this origin, a recorded run, or the Python engine in your browser.
 */
window.AQUASENTINEL_API = window.AQUASENTINEL_API || null;
