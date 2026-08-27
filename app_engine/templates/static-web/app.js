const button = document.querySelector("#hello");
const message = document.querySelector("#message");

button.addEventListener("click", () => {
  message.textContent = "Hello from {{APP_LABEL}}!";
});
