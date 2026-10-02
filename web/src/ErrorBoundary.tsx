import React from 'react'

type ErrorBoundaryState = {
  error: Error | null
}

export class ErrorBoundary extends React.Component<React.PropsWithChildren, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error }
  }

  render() {
    if (this.state.error) {
      return (
        <main className="flex min-h-screen items-center justify-center bg-[#f0f2f5] px-6 text-[#1f1f1f]">
          <section className="max-w-md rounded-2xl border border-[#e8e8e8] bg-white p-6 shadow-xl">
            <p className="text-sm font-medium text-[#ff4d4f]">Preview failed to render</p>
            <h1 className="mt-2 text-2xl font-semibold">Something went wrong.</h1>
            <p className="mt-3 text-sm text-[#666666]">
              The app caught a runtime error instead of rendering a blank screen.
            </p>
            <pre className="mt-4 max-h-40 overflow-auto rounded-lg bg-[#fafafa] p-3 text-xs text-[#1f1f1f] border border-[#f0f0f0]">
              {this.state.error.message}
            </pre>
          </section>
        </main>
      )
    }
    return this.props.children
  }
}
