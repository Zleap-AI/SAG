/** Profile-scoped command prefix that reaches the installed dsh-sag bin. */
export const SAG_PROFILE_CLI = 'dsh plugin --profile web exec dsh-sag'

/** Default local SAG discovery/setup command shown in recovery guidance. */
export const SAG_SETUP_COMMAND = `${SAG_PROFILE_CLI} setup`

/** Non-mutating connection diagnostic command shown in recovery guidance. */
export const SAG_DOCTOR_COMMAND = `${SAG_PROFILE_CLI} doctor`
