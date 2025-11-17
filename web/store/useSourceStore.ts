import { create } from 'zustand'
import { Source } from '@/types'

interface SourceStore {
  currentSource: Source | null
  sources: Source[]
  setCurrentSource: (source: Source | null) => void
  setSources: (sources: Source[]) => void
  addSource: (source: Source) => void
  updateSource: (id: string, data: Partial<Source>) => void
  removeSource: (id: string) => void
}

export const useSourceStore = create<SourceStore>((set) => ({
  currentSource: null,
  sources: [],
  
  setCurrentSource: (source) => set({ currentSource: source }),
  
  setSources: (sources) => set({ sources }),
  
  addSource: (source) => set((state) => ({
    sources: [...state.sources, source]
  })),
  
  updateSource: (id, data) => set((state) => ({
    sources: state.sources.map((s) =>
      s.id === id ? { ...s, ...data } : s
    ),
    currentSource: state.currentSource?.id === id
      ? { ...state.currentSource, ...data }
      : state.currentSource
  })),
  
  removeSource: (id) => set((state) => ({
    sources: state.sources.filter((s) => s.id !== id),
    currentSource: state.currentSource?.id === id ? null : state.currentSource
  })),
}))

