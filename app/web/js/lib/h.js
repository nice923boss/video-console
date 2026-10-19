// Single import point for Preact, its hooks and the htm tagged-template binding.
import { h, render, Fragment, createContext, toChildArray } from "../../vendor/preact.module.js";
import {
  useState, useEffect, useRef, useMemo, useCallback, useReducer, useLayoutEffect, useContext,
} from "../../vendor/hooks.module.js";
import htm from "../../vendor/htm.module.js";

export const html = htm.bind(h);

export {
  h, render, Fragment, createContext, toChildArray,
  useState, useEffect, useRef, useMemo, useCallback, useReducer, useLayoutEffect, useContext,
};
