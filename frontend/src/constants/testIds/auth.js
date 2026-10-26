// Test IDs for the auth feature (login, register, password reset, logout).
// Add new keys here as you wire up additional auth UI; see ./index.js for
// the recipe to add a new feature file.
//
// Directive:
//   - Keys are camelCase, values are kebab-case shaped as `<feature>-<element>`
//     (or `<feature>-<element>-<qualifier>` when an element repeats). Examples:
//     'login-submit-button', 'cart-quantity-input', 'product-card-image'.
//   - Reference them in JSX as `data-testid={LOGIN.submitButton}`.
//
// Why kebab-case values: required by qabot's CSS-attribute selector matcher
// and the kebab-case testid lint convention.

export const LOGIN = {
	emailInput: 'login-email-input',
	passwordInput: 'login-password-input',
	submitButton: 'login-submit-button',
	forgotPasswordLink: 'login-forgot-password-link',
	registerLink: 'login-register-link',
};

export const REGISTER = {
	nameInput: 'register-name-input',
	emailInput: 'register-email-input',
	passwordInput: 'register-password-input',
	passwordConfirmInput: 'register-password-confirm-input',
	submitButton: 'register-submit-button',
	loginLink: 'register-login-link',
};

export const LOGOUT = {
	button: 'logout-button',
};

export const PASSWORD_RESET = {
	emailInput: 'password-reset-email-input',
	submitButton: 'password-reset-submit-button',
	sentMessage: 'password-reset-sent-message',
	loginLink: 'password-reset-login-link',
};

export const RESET_PASSWORD = {
	passwordInput: 'reset-password-password-input',
	confirmInput: 'reset-password-confirm-input',
	submitButton: 'reset-password-submit-button',
	doneMessage: 'reset-password-done-message',
	invalidMessage: 'reset-password-invalid-message',
	loginLink: 'reset-password-login-link',
	requestLink: 'reset-password-request-link',
};
