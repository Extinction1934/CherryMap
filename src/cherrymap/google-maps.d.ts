export {};

declare global {
  namespace google.maps {
    class LatLng {
      constructor(lat: number | { lat: number; lng: number }, lng?: number);
      lat(): number;
      lng(): number;
    }
    class LatLngBounds {
      constructor(sw?: { lat: number; lng: number } | GeographicBoundsLike, ne?: { lat: number; lng: number });
      extend(point: { lat: number; lng: number } | LatLng): LatLngBounds;
      getNorthEast(): LatLng;
      getSouthWest(): LatLng;
      contains(point: { lat: number; lng: number } | LatLng): boolean;
      toJSON(): { south: number; north: number; west: number; east: number };
    }
    class Size {
      constructor(width: number, height: number);
    }
    class Point {
      constructor(x: number, y: number);
    }
    interface Icon {
      url: string;
      scaledSize?: Size;
      anchor?: Point;
      labelOrigin?: Point;
    }
    class Marker {
      constructor(options?: Record<string, unknown>);
      setMap(map: Map | null): void;
      setVisible(visible: boolean): void;
      setIcon(icon: Icon): void;
      setZIndex(zIndex: number): void;
      setLabel(label: Record<string, string> | null): void;
      setPosition(position: { lat: number; lng: number }): void;
      setTitle(title: string): void;
      getTitle(): string | undefined;
      addListener(eventName: string, handler: (...args: any[]) => void): MapsEventListener;
    }
    class Map {
      constructor(element: HTMLElement, options?: Record<string, unknown>);
      getZoom(): number | undefined;
      setZoom(zoom: number): void;
      getCenter(): LatLng | null | undefined;
      getBounds(): LatLngBounds | null | undefined;
      getProjection(): { fromLatLngToPoint(latLng: LatLng): { x: number; y: number } | null } | null | undefined;
      setMapTypeId(typeId: string): void;
      getTilt(): number;
      setTilt(tilt: number): void;
      getHeading(): number;
      setHeading(heading: number): void;
      panTo(position: { lat: number; lng: number }): void;
      fitBounds(
        bounds: LatLngBounds | { south: number; north: number; west: number; east: number },
        padding?: Record<string, number>,
      ): void;
      addListener(eventName: string, handler: (...args: any[]) => void): MapsEventListener;
    }
    class InfoWindow {
      constructor(options?: Record<string, unknown>);
      setContent(content: string | HTMLElement): void;
      setOptions(options: Record<string, unknown>): void;
      open(options: { map: Map; anchor?: Marker; shouldFocus?: boolean }): void;
      close(): void;
      addListener(eventName: string, handler: (...args: any[]) => void): MapsEventListener;
    }
    class Polyline {
      constructor(options?: Record<string, unknown>);
      setMap(map: Map | null): void;
    }
    interface MapMouseEvent {
      placeId?: string;
      stop?: () => void;
    }
    interface IconMouseEvent extends MapMouseEvent {}
    interface MapsEventListener {
      remove(): void;
    }
    const event: {
      clearInstanceListeners(instance: object): void;
    };
  }

  interface GeographicBoundsLike {
    south: number;
    north: number;
    west: number;
    east: number;
  }

  interface Window {
    google?: typeof google;
    __cherryMapReady?: () => void;
    gm_authFailure?: () => void;
  }
}
