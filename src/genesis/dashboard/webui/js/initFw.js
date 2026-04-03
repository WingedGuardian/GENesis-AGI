/**
 * Genesis standalone framework initializer.
 *
 * Stripped-down version of Agent Zero's initFw.js.
 * Only loads Alpine.js and registers custom directives.
 * No AZ-specific imports (modals, components, websocket, CSRF).
 */

// Import Alpine.js
await import("../vendor/alpine/alpine.min.js");

// Add x-destroy directive to alpine
Alpine.directive(
  "destroy",
  (_el, { expression }, { evaluateLater, cleanup }) => {
    const onDestroy = evaluateLater(expression);
    cleanup(() => onDestroy());
  }
);

// Add x-create directive to alpine
Alpine.directive(
  "create",
  (_el, { expression }, { evaluateLater }) => {
    const onCreate = evaluateLater(expression);
    onCreate();
  }
);
