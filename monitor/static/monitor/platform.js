// Role changes use the existing POST endpoint and its server-side validation.
document.querySelectorAll('[data-save-on-change]').forEach((form) => {
  form.addEventListener('change', (event) => {
    if (event.target.matches('select[name="role"]')) {
      form.requestSubmit();
    }
  });
});
