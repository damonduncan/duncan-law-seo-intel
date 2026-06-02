#!/usr/bin/env python3
"""One-time seed: load Duncan Law historical PPC (Google Ads) data into DiscoveryCache.

Source: Google Ads monthly reports (Dec 2021 – May 2024), 29 months.
Run once (or re-run to replace):
    DATABASE_URL="..." python scripts/seed_ppc_data.py
"""
import json
import os
import sys
import uuid
from datetime import datetime, timezone

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("Error: DATABASE_URL environment variable is not set.")
    sys.exit(1)

# ── Historical PPC data (Dec 2021 – May 2024) ────────────────────────────────
# Fields per market: impressions, clicks, ctr (%), spend ($), leads, cpl ($/lead)
# cpl omitted when leads=0 (would be division by zero in source sheet)

MONTHS = [
 {'year': 2021, 'month': 12,
  'charlotte':     {'impressions': 4551, 'clicks': 289,  'ctr':  6.35, 'spend': 3710.02, 'leads': 20, 'cpl': 185.50},
  'greensboro':    {'impressions': 2006, 'clicks': 136,  'ctr':  6.78, 'spend': 1611.00, 'leads': 23, 'cpl':  70.04},
  'winston_salem': {'impressions': 1161, 'clicks':  56,  'ctr':  4.82, 'spend':  659.31, 'leads': 11, 'cpl':  59.94},
  'salisbury':     {'impressions':   94, 'clicks':  11,  'ctr': 11.70, 'spend':  134.84, 'leads':  1, 'cpl': 134.84},
  'total':         {'impressions': 7812, 'clicks': 492,  'ctr':  6.30, 'spend': 6115.17, 'leads': 55, 'cpl': 111.18}},

 {'year': 2022, 'month': 1,
  'charlotte':     {'impressions': 5626, 'clicks': 394,  'ctr':  7.00, 'spend': 5173.57, 'leads': 36, 'cpl': 143.71},
  'greensboro':    {'impressions': 2174, 'clicks': 156,  'ctr':  7.18, 'spend': 1724.77, 'leads': 31, 'cpl':  55.64},
  'winston_salem': {'impressions': 1530, 'clicks': 102,  'ctr':  6.67, 'spend': 1349.49, 'leads': 10, 'cpl': 134.95},
  'salisbury':     {'impressions':  146, 'clicks':   6,  'ctr':  4.11, 'spend':   79.67, 'leads':  0},
  'total':         {'impressions': 9476, 'clicks': 658,  'ctr':  6.94, 'spend': 8327.50, 'leads': 77, 'cpl': 108.15}},

 # February 2022 — missing from source data

 {'year': 2022, 'month': 3,
  'charlotte':     {'impressions': 3736, 'clicks': 278,  'ctr':  7.44, 'spend': 3473.41, 'leads': 28, 'cpl': 124.05},
  'greensboro':    {'impressions': 2301, 'clicks': 145,  'ctr':  6.30, 'spend': 1786.63, 'leads': 30, 'cpl':  59.55},
  'winston_salem': {'impressions': 1464, 'clicks':  88,  'ctr':  6.01, 'spend': 1046.24, 'leads': 11, 'cpl':  95.11},
  'salisbury':     {'impressions':  178, 'clicks':  12,  'ctr':  6.74, 'spend':  174.29, 'leads':  0},
  'total':         {'impressions': 7679, 'clicks': 523,  'ctr':  6.81, 'spend': 6480.57, 'leads': 69, 'cpl':  93.92}},

 {'year': 2022, 'month': 4,
  'charlotte':     {'impressions': 3736, 'clicks': 278,  'ctr':  7.44, 'spend': 3473.41, 'leads': 29, 'cpl': 119.77},
  'greensboro':    {'impressions': 2157, 'clicks': 180,  'ctr':  8.34, 'spend': 2174.75, 'leads': 30, 'cpl':  72.49},
  'winston_salem': {'impressions': 1325, 'clicks': 120,  'ctr':  9.06, 'spend': 1442.21, 'leads': 23, 'cpl':  62.70},
  'salisbury':     {'impressions':  178, 'clicks':  12,  'ctr':  6.74, 'spend':  174.29, 'leads':  0},
  'total':         {'impressions': 7396, 'clicks': 590,  'ctr':  7.98, 'spend': 7264.66, 'leads': 82, 'cpl':  88.59}},

 {'year': 2022, 'month': 5,
  'charlotte':     {'impressions': 3184, 'clicks': 230,  'ctr':  7.22, 'spend': 2786.10, 'leads': 18, 'cpl': 154.78},
  'greensboro':    {'impressions': 2141, 'clicks': 183,  'ctr':  8.55, 'spend': 2260.26, 'leads': 26, 'cpl':  86.93},
  'winston_salem': {'impressions': 1401, 'clicks': 144,  'ctr': 10.28, 'spend': 1751.77, 'leads': 22, 'cpl':  79.63},
  'salisbury':     {'impressions':  175, 'clicks':  15,  'ctr':  8.57, 'spend':  199.67, 'leads':  1, 'cpl': 199.67},
  'total':         {'impressions': 6901, 'clicks': 572,  'ctr':  8.29, 'spend': 6997.80, 'leads': 67, 'cpl': 104.44}},

 {'year': 2022, 'month': 6,
  'charlotte':     {'impressions': 2786, 'clicks': 279,  'ctr': 10.01, 'spend': 2902.21, 'leads': 28, 'cpl': 103.65},
  'greensboro':    {'impressions': 2067, 'clicks': 236,  'ctr': 11.42, 'spend': 3033.18, 'leads': 41, 'cpl':  73.98},
  'winston_salem': {'impressions': 1816, 'clicks': 189,  'ctr': 10.41, 'spend': 2518.84, 'leads': 29, 'cpl':  86.86},
  'salisbury':     {'impressions':  200, 'clicks':  26,  'ctr': 13.00, 'spend':  450.43, 'leads':  9, 'cpl':  50.05},
  'total':         {'impressions': 6869, 'clicks': 730,  'ctr': 10.63, 'spend': 8904.66, 'leads': 107,'cpl':  83.22}},

 {'year': 2022, 'month': 7,
  'charlotte':     {'impressions': 2312, 'clicks': 250,  'ctr': 10.81, 'spend': 2702.93, 'leads': 18, 'cpl': 150.16},
  'greensboro':    {'impressions': 2051, 'clicks': 245,  'ctr': 11.95, 'spend': 3039.79, 'leads': 42, 'cpl':  72.38},
  'winston_salem': {'impressions': 1663, 'clicks': 171,  'ctr': 10.28, 'spend': 2269.78, 'leads': 15, 'cpl': 151.32},
  'salisbury':     {'impressions':  222, 'clicks':  23,  'ctr': 10.36, 'spend':  372.91, 'leads':  8, 'cpl':  46.61},
  'total':         {'impressions': 6248, 'clicks': 689,  'ctr': 11.03, 'spend': 8385.41, 'leads': 83, 'cpl': 101.03}},

 {'year': 2022, 'month': 8,
  'charlotte':     {'impressions': 2173, 'clicks': 260,  'ctr': 11.97, 'spend': 2630.02, 'leads': 29, 'cpl':  90.69},
  'greensboro':    {'impressions': 1949, 'clicks': 260,  'ctr': 13.34, 'spend': 3280.37, 'leads': 29, 'cpl': 113.12},
  'winston_salem': {'impressions': 1913, 'clicks': 185,  'ctr':  9.67, 'spend': 2404.64, 'leads': 24, 'cpl': 100.19},
  'salisbury':     {'impressions':  262, 'clicks':  42,  'ctr': 16.03, 'spend':  732.56, 'leads':  7, 'cpl': 104.65},
  'total':         {'impressions': 6297, 'clicks': 747,  'ctr': 11.86, 'spend': 9047.59, 'leads': 89, 'cpl': 101.66}},

 {'year': 2022, 'month': 9,
  'charlotte':     {'impressions': 2335, 'clicks': 248,  'ctr': 10.62, 'spend': 2733.97, 'leads': 24, 'cpl': 113.92},
  'greensboro':    {'impressions': 2034, 'clicks': 252,  'ctr': 12.39, 'spend': 3255.41, 'leads': 26, 'cpl': 125.21},
  'winston_salem': {'impressions': 1798, 'clicks': 208,  'ctr': 11.57, 'spend': 2709.90, 'leads': 22, 'cpl': 123.18},
  'salisbury':     {'impressions':  268, 'clicks':  34,  'ctr': 12.69, 'spend':  548.26, 'leads':  4, 'cpl': 137.07},
  'total':         {'impressions': 6435, 'clicks': 742,  'ctr': 11.53, 'spend': 9247.54, 'leads': 76, 'cpl': 121.68}},

 {'year': 2022, 'month': 10,
  'charlotte':     {'impressions': 2361, 'clicks': 255,  'ctr': 10.80, 'spend': 2888.98, 'leads': 29, 'cpl':  99.62},
  'greensboro':    {'impressions': 2223, 'clicks': 243,  'ctr': 10.93, 'spend': 3039.71, 'leads': 47, 'cpl':  64.67},
  'winston_salem': {'impressions': 2168, 'clicks': 225,  'ctr': 10.38, 'spend': 2786.58, 'leads': 25, 'cpl': 111.46},
  'salisbury':     {'impressions':  286, 'clicks':  41,  'ctr': 14.34, 'spend':  577.09, 'leads':  5, 'cpl': 115.42},
  'total':         {'impressions': 7038, 'clicks': 764,  'ctr': 10.86, 'spend': 9292.36, 'leads': 106,'cpl':  87.66}},

 {'year': 2022, 'month': 11,
  'charlotte':     {'impressions': 2951, 'clicks': 315,  'ctr': 10.67, 'spend': 3365.75, 'leads': 25, 'cpl': 134.63},
  'greensboro':    {'impressions': 2405, 'clicks': 237,  'ctr':  9.85, 'spend': 2999.37, 'leads': 27, 'cpl': 111.09},
  'winston_salem': {'impressions': 1815, 'clicks': 203,  'ctr': 11.18, 'spend': 2520.00, 'leads': 35, 'cpl':  72.00},
  'salisbury':     {'impressions':  311, 'clicks':  39,  'ctr': 12.54, 'spend':  617.94, 'leads':  5, 'cpl': 123.59},
  'total':         {'impressions': 7482, 'clicks': 794,  'ctr': 10.61, 'spend': 9503.06, 'leads': 92, 'cpl': 103.29}},

 {'year': 2022, 'month': 12,
  'charlotte':     {'impressions': 2708, 'clicks': 294,  'ctr': 10.86, 'spend': 2954.56, 'leads': 23, 'cpl': 128.46},
  'greensboro':    {'impressions': 2595, 'clicks': 235,  'ctr':  9.06, 'spend': 2894.71, 'leads': 24, 'cpl': 120.61},
  'winston_salem': {'impressions': 2045, 'clicks': 204,  'ctr':  9.98, 'spend': 2468.96, 'leads': 23, 'cpl': 107.35},
  'salisbury':     {'impressions':  254, 'clicks':  31,  'ctr': 12.20, 'spend':  471.29, 'leads':  5, 'cpl':  94.26},
  'total':         {'impressions': 7602, 'clicks': 764,  'ctr': 10.05, 'spend': 8789.52, 'leads': 75, 'cpl': 117.19}},

 {'year': 2023, 'month': 1,
  'charlotte':     {'impressions': 2735, 'clicks': 251,  'ctr':  9.18, 'spend': 2593.39, 'leads': 31, 'cpl':  83.66},
  'greensboro':    {'impressions': 2260, 'clicks': 251,  'ctr': 11.11, 'spend': 3039.41, 'leads': 33, 'cpl':  92.10},
  'winston_salem': {'impressions': 2731, 'clicks': 256,  'ctr':  9.37, 'spend': 3038.86, 'leads': 32, 'cpl':  94.96},
  'salisbury':     {'impressions':  387, 'clicks':  53,  'ctr': 13.70, 'spend':  845.63, 'leads':  9, 'cpl':  93.96},
  'total':         {'impressions': 8113, 'clicks': 811,  'ctr': 10.00, 'spend': 9517.29, 'leads': 105,'cpl':  90.64}},

 {'year': 2023, 'month': 2,
  'charlotte':     {'impressions': 2584, 'clicks': 284,  'ctr': 10.99, 'spend': 2593.39, 'leads': 20, 'cpl': 129.67},
  'greensboro':    {'impressions': 2282, 'clicks': 256,  'ctr': 11.22, 'spend': 3053.52, 'leads': 37, 'cpl':  82.53},
  'winston_salem': {'impressions': 2334, 'clicks': 234,  'ctr': 10.03, 'spend': 2846.95, 'leads': 35, 'cpl':  81.34},
  'salisbury':     {'impressions':  357, 'clicks':  50,  'ctr': 14.01, 'spend':  776.47, 'leads':  3, 'cpl': 258.82},
  'total':         {'impressions': 7557, 'clicks': 824,  'ctr': 10.90, 'spend': 9270.33, 'leads': 95, 'cpl':  97.58}},

 {'year': 2023, 'month': 3,
  'charlotte':     {'impressions': 2449, 'clicks': 257,  'ctr': 10.49, 'spend': 2583.61, 'leads': 33, 'cpl':  78.29},
  'greensboro':    {'impressions': 2167, 'clicks': 262,  'ctr': 12.09, 'spend': 3030.88, 'leads': 25, 'cpl': 121.24},
  'winston_salem': {'impressions': 1867, 'clicks': 224,  'ctr': 12.00, 'spend': 2578.92, 'leads': 41, 'cpl':  62.90},
  'salisbury':     {'impressions':  343, 'clicks':  51,  'ctr': 14.87, 'spend':  659.91, 'leads':  9, 'cpl':  73.32},
  'total':         {'impressions': 6826, 'clicks': 794,  'ctr': 11.63, 'spend': 8853.32, 'leads': 108,'cpl':  81.98}},

 {'year': 2023, 'month': 4,
  'charlotte':     {'impressions': 2094, 'clicks': 268,  'ctr': 12.44, 'spend': 2583.75, 'leads': 25, 'cpl': 103.35},
  'greensboro':    {'impressions': 2108, 'clicks': 258,  'ctr': 12.24, 'spend': 3032.88, 'leads': 21, 'cpl': 144.42},
  'winston_salem': {'impressions': 1863, 'clicks': 219,  'ctr': 11.76, 'spend': 2588.62, 'leads': 53, 'cpl':  48.84},
  'salisbury':     {'impressions':  357, 'clicks':  54,  'ctr': 15.13, 'spend':  744.86, 'leads':  4, 'cpl': 186.22},
  'total':         {'impressions': 6422, 'clicks': 799,  'ctr': 12.44, 'spend': 8950.11, 'leads': 103,'cpl':  86.89}},

 {'year': 2023, 'month': 5,
  'charlotte':     {'impressions': 1996, 'clicks': 258,  'ctr': 12.93, 'spend': 2605.91, 'leads': 12, 'cpl': 217.16},
  'greensboro':    {'impressions': 1855, 'clicks': 258,  'ctr': 13.91, 'spend': 3038.00, 'leads': 35, 'cpl':  86.80},
  'winston_salem': {'impressions': 2055, 'clicks': 244,  'ctr': 11.87, 'spend': 2883.96, 'leads': 43, 'cpl':  67.07},
  'salisbury':     {'impressions':  337, 'clicks':  43,  'ctr': 12.76, 'spend':  547.44, 'leads':  6, 'cpl':  91.24},
  'total':         {'impressions': 6243, 'clicks': 803,  'ctr': 12.86, 'spend': 9075.31, 'leads': 96, 'cpl':  94.53}},

 {'year': 2023, 'month': 6,
  'charlotte':     {'impressions': 2879, 'clicks': 321,  'ctr': 11.15, 'spend': 3266.80, 'leads': 33, 'cpl':  98.99},
  'greensboro':    {'impressions': 1748, 'clicks': 253,  'ctr': 14.47, 'spend': 3031.30, 'leads': 48, 'cpl':  63.15},
  'winston_salem': {'impressions': 1929, 'clicks': 241,  'ctr': 12.49, 'spend': 3044.58, 'leads': 41, 'cpl':  74.26},
  'salisbury':     {'impressions':  452, 'clicks':  58,  'ctr': 12.83, 'spend':  734.91, 'leads':  8, 'cpl':  91.86},
  'total':         {'impressions': 7008, 'clicks': 873,  'ctr': 12.46, 'spend': 10077.59,'leads': 130,'cpl':  77.52}},

 {'year': 2023, 'month': 7,
  'charlotte':     {'impressions': 2885, 'clicks': 345,  'ctr': 11.96, 'spend': 3495.87, 'leads': 47, 'cpl':  74.38},
  'greensboro':    {'impressions': 2483, 'clicks': 273,  'ctr': 10.99, 'spend': 3043.53, 'leads': 35, 'cpl':  86.96},
  'winston_salem': {'impressions': 1937, 'clicks': 261,  'ctr': 13.47, 'spend': 3039.97, 'leads': 43, 'cpl':  70.70},
  'salisbury':     {'impressions':  506, 'clicks':  43,  'ctr':  8.50, 'spend':  539.36, 'leads':  8, 'cpl':  67.42},
  'total':         {'impressions': 7811, 'clicks': 922,  'ctr': 11.80, 'spend': 10118.73,'leads': 133,'cpl':  76.08}},

 {'year': 2023, 'month': 8,
  'charlotte':     {'impressions': 3588, 'clicks': 391,  'ctr': 10.90, 'spend': 3373.28, 'leads': 88, 'cpl':  38.33},
  'greensboro':    {'impressions': 2092, 'clicks': 255,  'ctr': 12.19, 'spend': 3043.98, 'leads': 41, 'cpl':  74.24},
  'winston_salem': {'impressions': 1896, 'clicks': 250,  'ctr': 13.19, 'spend': 3060.64, 'leads': 47, 'cpl':  65.12},
  'salisbury':     {'impressions':  513, 'clicks':  79,  'ctr': 15.40, 'spend': 1017.01, 'leads':  8, 'cpl': 127.13},
  'total':         {'impressions': 8089, 'clicks': 975,  'ctr': 12.05, 'spend': 10494.91,'leads': 184,'cpl':  57.04}},

 {'year': 2023, 'month': 9,
  'charlotte':     {'impressions': 3434, 'clicks': 371,  'ctr': 10.80, 'spend': 3157.06, 'leads': 74, 'cpl':  42.66},
  'greensboro':    {'impressions': 1817, 'clicks': 235,  'ctr': 12.93, 'spend': 3177.86, 'leads': 61, 'cpl':  52.10},
  'winston_salem': {'impressions': 1655, 'clicks': 229,  'ctr': 13.84, 'spend': 3086.68, 'leads': 55, 'cpl':  56.12},
  'salisbury':     {'impressions':  371, 'clicks':  47,  'ctr': 12.67, 'spend':  551.09, 'leads':  5, 'cpl': 110.22},
  'total':         {'impressions': 7277, 'clicks': 882,  'ctr': 12.12, 'spend': 9972.69, 'leads': 195,'cpl':  51.14}},

 {'year': 2023, 'month': 10,
  'charlotte':     {'impressions': 4099, 'clicks': 560,  'ctr': 13.66, 'spend': 3618.55, 'leads': 72, 'cpl':  50.26},
  'greensboro':    {'impressions': 2394, 'clicks': 332,  'ctr': 13.87, 'spend': 3799.99, 'leads': 72, 'cpl':  52.78},
  'winston_salem': {'impressions': 1880, 'clicks': 309,  'ctr': 16.44, 'spend': 3519.41, 'leads': 28, 'cpl': 125.69},
  'salisbury':     {'impressions':  446, 'clicks':  48,  'ctr': 10.76, 'spend':  636.80, 'leads':  7, 'cpl':  90.97},
  'total':         {'impressions': 8819, 'clicks': 1249, 'ctr': 14.16, 'spend': 11574.75,'leads': 179,'cpl':  64.66}},

 {'year': 2023, 'month': 11,
  'charlotte':     {'impressions': 3087, 'clicks': 377,  'ctr': 12.21, 'spend': 2736.08, 'leads': 45, 'cpl':  60.80},
  'greensboro':    {'impressions': 2052, 'clicks': 331,  'ctr': 16.13, 'spend': 3248.35, 'leads': 51, 'cpl':  63.69},
  'winston_salem': {'impressions': 1347, 'clicks': 197,  'ctr': 14.63, 'spend': 1520.08, 'leads': 20, 'cpl':  76.00},
  'salisbury':     {'impressions':  386, 'clicks':  49,  'ctr': 12.69, 'spend':  727.92, 'leads': 12, 'cpl':  60.66},
  'total':         {'impressions': 6872, 'clicks': 954,  'ctr': 13.88, 'spend': 8232.43, 'leads': 128,'cpl':  64.32}},

 {'year': 2023, 'month': 12,
  'charlotte':     {'impressions': 3214, 'clicks': 394,  'ctr': 12.26, 'spend': 2736.15, 'leads': 64, 'cpl':  42.75},
  'greensboro':    {'impressions': 1796, 'clicks': 297,  'ctr': 16.54, 'spend': 3040.07, 'leads': 45, 'cpl':  67.56},
  'winston_salem': {'impressions': 1076, 'clicks': 177,  'ctr': 16.45, 'spend': 1518.73, 'leads': 28, 'cpl':  54.24},
  'salisbury':     {'impressions':  382, 'clicks':  65,  'ctr': 17.02, 'spend':  958.26, 'leads':  9, 'cpl': 106.47},
  'total':         {'impressions': 6468, 'clicks': 933,  'ctr': 14.42, 'spend': 8253.21, 'leads': 146,'cpl':  56.53}},

 {'year': 2024, 'month': 1,
  'charlotte':     {'impressions': 4211, 'clicks': 537,  'ctr': 12.75, 'spend': 2733.70, 'leads': 94, 'cpl':  29.08},
  'greensboro':    {'impressions': 1861, 'clicks': 291,  'ctr': 15.64, 'spend': 3046.72, 'leads': 44, 'cpl':  69.24},
  'winston_salem': {'impressions': 1100, 'clicks': 169,  'ctr': 15.36, 'spend': 1520.05, 'leads': 32, 'cpl':  47.50},
  'salisbury':     {'impressions':  446, 'clicks':  76,  'ctr': 17.04, 'spend': 1088.66, 'leads': 11, 'cpl':  98.97},
  'total':         {'impressions': 7618, 'clicks': 1073, 'ctr': 14.09, 'spend': 8389.13, 'leads': 181,'cpl':  46.35}},

 {'year': 2024, 'month': 2,
  'charlotte':     {'impressions': 3825, 'clicks': 505,  'ctr': 13.20, 'spend': 2736.15, 'leads': 91, 'cpl':  30.07},
  'greensboro':    {'impressions': 1942, 'clicks': 286,  'ctr': 14.73, 'spend': 3040.13, 'leads': 26, 'cpl': 116.93},
  'winston_salem': {'impressions': 1165, 'clicks': 167,  'ctr': 14.33, 'spend': 1523.74, 'leads': 39, 'cpl':  39.07},
  'salisbury':     {'impressions':  519, 'clicks':  73,  'ctr': 14.07, 'spend': 1089.35, 'leads':  9, 'cpl': 121.04},
  'total':         {'impressions': 7451, 'clicks': 1031, 'ctr': 13.84, 'spend': 8389.37, 'leads': 165,'cpl':  50.84}},

 {'year': 2024, 'month': 3,
  'charlotte':     {'impressions': 4233, 'clicks': 644,  'ctr': 15.21, 'spend': 3038.57, 'leads': 83, 'cpl':  36.61},
  'greensboro':    {'impressions': 1275, 'clicks': 267,  'ctr': 20.94, 'spend': 3174.00, 'leads': 49, 'cpl':  64.78},
  'winston_salem': {'impressions': 1056, 'clicks': 166,  'ctr': 15.72, 'spend': 1513.24, 'leads': 42, 'cpl':  36.03},
  'salisbury':     {'impressions':  120, 'clicks':  22,  'ctr': 18.33, 'spend':  349.12, 'leads':  4, 'cpl':  87.28},
  'total':         {'impressions': 6684, 'clicks': 1099, 'ctr': 16.44, 'spend': 8074.93, 'leads': 178,'cpl':  45.36}},

 {'year': 2024, 'month': 4,
  'charlotte':     {'impressions': 4204, 'clicks': 662,  'ctr': 15.75, 'spend': 3648.08, 'leads': 123,'cpl':  29.66},
  'greensboro':    {'impressions': 1460, 'clicks': 272,  'ctr': 18.63, 'spend': 3644.72, 'leads': 64, 'cpl':  56.95},
  'winston_salem': {'impressions': 1036, 'clicks': 159,  'ctr': 15.35, 'spend': 1520.12, 'leads': 18, 'cpl':  84.45},
  'salisbury':     {'impressions':  182, 'clicks':  31,  'ctr': 17.03, 'spend':  427.25, 'leads':  4, 'cpl': 106.81},
  'total':         {'impressions': 6882, 'clicks': 1124, 'ctr': 16.33, 'spend': 9240.17, 'leads': 209,'cpl':  44.21}},

 {'year': 2024, 'month': 5,
  'charlotte':     {'impressions': 4031, 'clicks': 593,  'ctr': 14.71, 'spend': 3648.12, 'leads': 99, 'cpl':  36.85},
  'greensboro':    {'impressions': 1400, 'clicks': 268,  'ctr': 19.14, 'spend': 3664.45, 'leads': 47, 'cpl':  77.97},
  'winston_salem': {'impressions':  542, 'clicks': 130,  'ctr': 23.99, 'spend': 1519.95, 'leads': 19, 'cpl':  80.00},
  'salisbury':     {'impressions':  144, 'clicks':  35,  'ctr': 24.31, 'spend':  472.13, 'leads': 11, 'cpl':  42.92},
  'total':         {'impressions': 6117, 'clicks': 1026, 'ctr': 16.77, 'spend': 9304.65, 'leads': 176,'cpl':  52.87}},
]

MONTH_NAMES = ['', 'January','February','March','April','May','June',
               'July','August','September','October','November','December']

payload = {
    "months":     MONTHS,
    "source":     "Google Ads monthly reports via PPC provider",
    "date_range": "December 2021 – May 2024 (29 months; February 2022 missing)",
    "markets":    ["charlotte", "greensboro", "winston_salem", "salisbury"],
    "seeded_at":  datetime.now(timezone.utc).isoformat(),
}

try:
    from sqlalchemy import create_engine, text
except ImportError:
    print("Error: sqlalchemy not installed. Run: pip install sqlalchemy psycopg2-binary")
    sys.exit(1)

engine = create_engine(DATABASE_URL)
with engine.begin() as conn:
    existing = conn.execute(
        text("SELECT id FROM discovery_cache WHERE key = 'ppc_monthly_data'")
    ).fetchone()
    if existing:
        conn.execute(
            text("UPDATE discovery_cache SET value = :v, updated_at = :t WHERE key = 'ppc_monthly_data'"),
            {"v": json.dumps(payload), "t": datetime.now(timezone.utc)},
        )
        print(f"Updated ppc_monthly_data ({len(MONTHS)} months)")
    else:
        conn.execute(
            text("INSERT INTO discovery_cache (id, key, value, updated_at) VALUES (:id, :k, :v, :t)"),
            {"id": str(uuid.uuid4()), "k": "ppc_monthly_data",
             "v": json.dumps(payload), "t": datetime.now(timezone.utc)},
        )
        print(f"Inserted ppc_monthly_data ({len(MONTHS)} months)")

print("Done.")
