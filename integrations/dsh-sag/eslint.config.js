import tseslint from 'typescript-eslint'

export default tseslint.config(
  {
    ignores: ['**/lib/**', '**/runtime/**'],
  },
  ...tseslint.configs.recommended,
  {
    files: ['packages/dsh-sag/src/**/*.ts'],
    rules: {
      '@typescript-eslint/consistent-type-imports': 'error',
      '@typescript-eslint/no-explicit-any': 'error',
    },
  },
)
