# -*- coding: utf-8 -*-
"""模块化拆分 v1.2.13：sf_icons —— 由 stock_float_window.py 搬移生成，行为零变化。"""

import math
import os
import queue
import threading
from PyQt6.QtCore import (QPointF, QRectF, Qt)
from PyQt6.QtGui import (QColor, QGuiApplication, QIcon, QImage, QLinearGradient, QPainter, QPainterPath,
                         QPixmap, QPen)
from sf_constants import (ICON_PATH, BTN_ICON_PT, BTN_ICON_DEVICE_RATIO,
                          MENU_ICON_PT, MENU_ICON_CONTENT_SCALE, MENU_ICON_DEVICE_RATIO)
from sf_logging import log_line, log_swallow

# 主界面与各弹窗共用的顶部按钮 SVG；弹窗禁止再用近似自绘造型。
_SEARCH_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
    'viewBox="0 0 24 24" fill="none" stroke="#EFEFEF" stroke-width="2.8" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="10.5" cy="10.5" r="4.5"/>'
    '<path d="m14 14 4.5 4.5"/></svg>'
)

_ICON_SVG = {
    "edit": _SEARCH_ICON_SVG,
    "close": '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" fill="none" version="1.1" width="24" height="24" viewBox="0 0 24 24"><defs><clipPath id="master_svg0_sw52_04555"><rect x="0" y="0" width="24" height="24" rx="0"/></clipPath></defs><g clip-path="url(#master_svg0_sw52_04555)"><path d="M12.000000499999999,13.96777345C12.000000499999999,13.96777345,7.8673718,18.100402250000002,7.8673718,18.100402250000002C7.5895481,18.37822625,7.2615615,18.515209249999998,6.8834126,18.511349250000002C6.5052636,18.50749125,6.17727739,18.366649250000002,5.89945343,18.08882625C5.62934715,17.81100225,5.4962230898,17.484945250000003,5.500081693965,17.11065525C5.5039407401,16.73636525,5.64092295,16.41416625,5.9110297,16.14406125C5.9110297,16.14406125,10.0320816,11.99985505,10.0320816,11.99985505C10.0320816,11.99985505,5.9110297,7.855650450000001,5.9110297,7.855650450000001C5.64092295,7.58554455,5.5058700419,7.26334535,5.5058700419,6.88905635C5.5058700419,6.51476535,5.64092295,6.18870854,5.9110297,5.91088367C6.18113595,5.6330606,6.5052636,5.4922202239,6.8834126,5.488360736017C7.2615615,5.4845012482,7.5895481,5.62148392,7.8673718,5.89930874C7.8673718,5.89930874,12.000000499999999,10.031937150000001,12.000000499999999,10.031937150000001C12.000000499999999,10.031937150000001,16.132628,5.89930874,16.132628,5.89930874C16.410454,5.62148392,16.73844,5.4845012482,17.116588,5.488360736017C17.494737,5.4922202239,17.822724,5.6330606,18.100547,5.91088367C18.370655,6.18870854,18.503778,6.51476535,18.499918,6.88905635C18.49606,7.26334535,18.359077,7.58554455,18.088972,7.855650450000001C18.088972,7.855650450000001,13.9679184,11.99985505,13.9679184,11.99985505C13.9679184,11.99985505,18.088972,16.14406125,18.088972,16.14406125C18.359077,16.41416625,18.494131,16.73636525,18.494131,17.11065525C18.494131,17.484945250000003,18.359077,17.81100225,18.088972,18.08882625C17.818865000000002,18.366649250000002,17.494737,18.50749125,17.116588,18.511349250000002C16.73844,18.515209249999998,16.410454,18.37822625,16.132628,18.100402250000002C16.132628,18.100402250000002,12.000000499999999,13.96777345,12.000000499999999,13.96777345Z" fill="#EFEFEF" fill-opacity="1"/></g></svg>',
    "min": '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" fill="none" version="1.1" width="24" height="24" viewBox="0 0 24 24"><defs><clipPath id="master_svg0_sw52_04542"><rect x="0" y="0" width="24" height="24" rx="0"/></clipPath></defs><g clip-path="url(#master_svg0_sw52_04542)"><path d="M6.317112,14.3920364C5.8096687,14.3920364,5.36953878,14.2056277,4.99672359,13.832813C4.62390786,13.4599977,4.4375,13.019867699999999,4.4375,12.5124249C4.4375,12.0049812,4.62390786,11.56485128,4.99672359,11.19203669C5.36953878,10.819220959999999,5.8096687,10.6328125,6.317112,10.6328125C6.317112,10.6328125,18.557886,10.6328125,18.557886,10.6328125C19.065329,10.6328125,19.505463,10.819220959999999,19.878275000000002,11.19203669C20.251089,11.56485128,20.4375,12.0049812,20.4375,12.5124249C20.4375,13.019867699999999,20.251089,13.4599977,19.878275000000002,13.832813C19.505463,14.2056277,19.065329,14.3920364,18.557886,14.3920364C18.557886,14.3920364,6.317112,14.3920364,6.317112,14.3920364Z" fill="#EFEFEF" fill-opacity="1"/></g></svg>',
}

# ---- 右键菜单行首图标（2026-10-13，用户看图要求）----
# 与顶部三按钮同一族做法：SVG 内嵌在本模块、运行时用 QSvgRenderer 出图，
# **不往包里加 .svg 文件**。为什么不走文件：加文件要同时管 StockFloat.spec 的
# datas、bundle_closure_check 的闭包、以及"文件没进包时菜单长什么样"三件事，
# 而内嵌这条路已经有 v1.4.11（首帧前同步渲染）与 v1.4.59（出图必须标 DPR）
# 两轮实测口径可以直接沿用。取色 #EFEFEF 与上面那三枚同源（同一位设计者的图标包）。
_MENU_ICON_SVG = {
    'refresh': '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"> <path d="M11.9434 20.4805C9.58756 20.4805 7.58522 19.656 5.93639 18.007C4.28739 16.3582 3.46289 14.3559 3.46289 12C3.46289 9.6442 4.28739 7.64186 5.93639 5.99303C7.58522 4.34403 9.58756 3.51953 11.9434 3.51953C13.1782 3.51953 14.3512 3.7712 15.4624 4.27453C16.5736 4.7777 17.5231 5.50395 18.3109 6.45328V4.63253C18.3109 4.32103 18.4185 4.05761 18.6336 3.84228C18.8488 3.62711 19.1122 3.51953 19.4239 3.51953C19.7354 3.51953 19.9988 3.62711 20.2141 3.84228C20.4293 4.05761 20.5369 4.32103 20.5369 4.63253V9.88703C20.5369 10.2552 20.4081 10.568 20.1504 10.8255C19.8929 11.0832 19.5801 11.212 19.2119 11.212H13.9291C13.6176 11.212 13.3566 11.1044 13.1461 10.8893C12.9356 10.6739 12.8304 10.4105 12.8304 10.099C12.8304 9.78736 12.938 9.52628 13.1531 9.31578C13.3683 9.10528 13.6317 9.00003 13.9434 9.00003H16.9456C16.4123 8.1232 15.7043 7.43228 14.8216 6.92728C13.939 6.42211 12.9796 6.16953 11.9434 6.16953C10.3239 6.16953 8.94722 6.73636 7.81339 7.87003C6.67972 9.00386 6.11289 10.3805 6.11289 12C6.11289 13.6195 6.67972 14.9962 7.81339 16.13C8.94722 17.2637 10.3239 17.8305 11.9434 17.8305C12.9731 17.8305 13.9234 17.5784 14.7944 17.074C15.6654 16.5697 16.364 15.8954 16.8901 15.051C17.08 14.7612 17.3428 14.5587 17.6786 14.4435C18.0145 14.3284 18.3451 14.3313 18.6706 14.4523C19.0126 14.5733 19.2608 14.7954 19.4151 15.1185C19.5695 15.4417 19.5611 15.7482 19.3901 16.038C18.6503 17.3809 17.6211 18.4569 16.3026 19.2663C14.9841 20.0758 13.5311 20.4805 11.9434 20.4805Z" fill="#EFEFEF"/> </svg>',
    'eye_open': '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"> <path d="M15.1876 14.6875C16.0626 13.8125 16.5001 12.75 16.5001 11.5C16.5001 10.25 16.0626 9.18751 15.1876 8.31251C14.3126 7.43751 13.2501 7.00001 12.0001 7.00001C10.7501 7.00001 9.68763 7.43751 8.81263 8.31251C7.93763 9.18751 7.50013 10.25 7.50013 11.5C7.50013 12.75 7.93763 13.8125 8.81263 14.6875C9.68763 15.5625 10.7501 16 12.0001 16C13.2501 16 14.3126 15.5625 15.1876 14.6875ZM10.2996 13.2005C9.83113 12.732 9.59688 12.1652 9.59688 11.5C9.59688 10.8348 9.83113 10.268 10.2996 9.79951C10.7681 9.33101 11.335 9.09676 12.0001 9.09676C12.6653 9.09676 13.2321 9.33101 13.7006 9.79951C14.1691 10.268 14.4034 10.8348 14.4034 11.5C14.4034 12.1652 14.1691 12.732 13.7006 13.2005C13.2321 13.669 12.6653 13.9033 12.0001 13.9033C11.335 13.9033 10.7681 13.669 10.2996 13.2005ZM5.71813 17.6098C3.81996 16.3909 2.32813 14.7699 1.24263 12.7468C1.13096 12.5496 1.04954 12.346 0.998375 12.136C0.947375 11.9258 0.921875 11.7138 0.921875 11.5C0.921875 11.2862 0.947375 11.0742 0.998375 10.864C1.04954 10.654 1.13096 10.4504 1.24263 10.2533C2.32813 8.2301 3.81996 6.6091 5.71813 5.39026C7.61629 4.17143 9.71029 3.56201 12.0001 3.56201C14.29 3.56201 16.384 4.17143 18.2821 5.39026C20.1803 6.6091 21.6721 8.2301 22.7576 10.2533C22.8693 10.4504 22.9507 10.654 23.0019 10.864C23.0529 11.0742 23.0784 11.2862 23.0784 11.5C23.0784 11.7138 23.0529 11.9258 23.0019 12.136C22.9507 12.346 22.8693 12.5496 22.7576 12.7468C21.6721 14.7699 20.1803 16.3909 18.2821 17.6098C16.384 18.8286 14.29 19.438 12.0001 19.438C9.71029 19.438 7.61629 18.8286 5.71813 17.6098Z" fill="#EFEFEF"/> </svg>',
    'eye_closed': '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"> <path d="M18.8599 21.9991L15.6849 18.8598C15.0922 19.062 14.4865 19.2089 13.8676 19.3006C13.2488 19.3922 12.6263 19.4381 12.0001 19.4381C9.71029 19.4381 7.61213 18.8287 5.70563 17.6098C3.79913 16.391 2.30671 14.77 1.22838 12.7468C1.11671 12.5497 1.03771 12.3484 0.991375 12.1431C0.945042 11.9376 0.921875 11.7232 0.921875 11.5001C0.921875 11.2769 0.945042 11.0626 0.991375 10.8571C1.03771 10.6517 1.11671 10.4505 1.22838 10.2533C1.56671 9.641 1.93479 9.04533 2.33263 8.46633C2.73046 7.88733 3.18554 7.35616 3.69788 6.87283L1.76412 4.92833C1.55262 4.71666 1.44688 4.46158 1.44688 4.16308C1.44688 3.86458 1.55629 3.60591 1.77513 3.38708C1.98679 3.17541 2.24604 3.06958 2.55288 3.06958C2.85971 3.06958 3.11896 3.17541 3.33063 3.38708L20.4011 20.4576C20.6128 20.6692 20.7205 20.9243 20.7241 21.2228C20.7276 21.5215 20.62 21.7802 20.4011 21.9991C20.1896 22.2106 19.9328 22.3163 19.6306 22.3163C19.3285 22.3163 19.0715 22.2106 18.8599 21.9991ZM12.0001 16.0001C12.1363 16.0001 12.2665 15.9941 12.3909 15.9821C12.5152 15.9702 12.6419 15.944 12.7709 15.9033L7.56862 10.7293C7.53746 10.8583 7.51821 10.985 7.51088 11.1093C7.50371 11.2337 7.50013 11.3639 7.50013 11.5001C7.50013 12.7501 7.93763 13.8126 8.81263 14.6876C9.68763 15.5626 10.7501 16.0001 12.0001 16.0001ZM12.0001 3.56208C14.29 3.56208 16.3923 4.17566 18.3071 5.40283C20.222 6.63 21.7185 8.25991 22.7969 10.2926C22.899 10.4824 22.9757 10.6777 23.0269 10.8783C23.0779 11.079 23.1034 11.2862 23.1034 11.5001C23.1034 11.7139 23.082 11.9235 23.0394 12.1288C22.9965 12.3343 22.924 12.532 22.8219 12.7218C22.5524 13.2443 22.2547 13.7452 21.9289 14.2246C21.6032 14.7039 21.2411 15.157 20.8426 15.5838C20.5528 15.9018 20.2 16.0549 19.7844 16.0431C19.3689 16.0311 19.0067 15.8707 18.6979 15.5621L16.6839 13.5478C16.5295 13.3935 16.431 13.2137 16.3881 13.0083C16.3455 12.8028 16.3491 12.5932 16.3991 12.3793C16.4375 12.2287 16.4639 12.0839 16.4784 11.9451C16.4929 11.8064 16.5001 11.6581 16.5001 11.5001C16.5001 10.2501 16.0626 9.18758 15.1876 8.31258C14.3126 7.43758 13.2501 7.00008 12.0001 7.00008C11.8421 7.00008 11.6938 7.00966 11.5551 7.02883C11.4163 7.048 11.2715 7.07683 11.1209 7.11533C10.907 7.15583 10.6956 7.15475 10.4866 7.11208C10.2775 7.06925 10.0957 6.97066 9.94138 6.81633L9.31438 6.18933C8.90338 5.77833 8.77088 5.29858 8.91688 4.75008C9.06304 4.20158 9.41038 3.86425 9.95888 3.73808C10.3002 3.68308 10.6386 3.64 10.9741 3.60883C11.3096 3.57766 11.6516 3.56208 12.0001 3.56208ZM13.7209 9.73483C13.8194 9.84783 13.9029 9.97233 13.9714 10.1083C14.0399 10.2442 14.086 10.3877 14.1099 10.5391C14.1265 10.6159 14.0954 10.6722 14.0164 10.7081C13.9374 10.7441 13.8667 10.7309 13.8044 10.6686L12.7436 9.60758C12.6813 9.54541 12.6699 9.47483 12.7094 9.39583C12.7489 9.31683 12.8117 9.282 12.8979 9.29133C13.0639 9.31533 13.2143 9.36516 13.3491 9.44083C13.4838 9.5165 13.6077 9.6145 13.7209 9.73483Z" fill="#EFEFEF"/> </svg>',
    'update': '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"> <path d="M4.09238 11.788C4.09238 12.622 4.25738 13.3782 4.58738 14.0565C4.91755 14.7347 5.34855 15.3347 5.88038 15.8565V14.8588C5.88038 14.5471 5.98797 14.286 6.20313 14.0755C6.41847 13.865 6.68188 13.7598 6.99338 13.7598C7.30505 13.7598 7.56613 13.865 7.77663 14.0755C7.98713 14.286 8.09238 14.5471 8.09238 14.8588V18.7173C8.09238 19.0855 7.96355 19.3984 7.70588 19.656C7.44838 19.9135 7.13555 20.0423 6.76738 20.0423H2.92288C2.61122 20.0423 2.35013 19.9347 2.13963 19.7195C1.92913 19.5044 1.82388 19.241 1.82388 18.9293C1.82388 18.6178 1.93147 18.3568 2.14663 18.1463C2.36197 17.9358 2.62538 17.8305 2.93688 17.8305H4.09663C3.26547 17.0595 2.6153 16.1597 2.14613 15.131C1.67697 14.1024 1.44238 12.988 1.44238 11.788C1.44238 10.101 1.89022 8.58421 2.78588 7.23754C3.68155 5.89071 4.85363 4.87454 6.30213 4.18904C6.60147 4.03688 6.90205 4.05396 7.20388 4.24029C7.50572 4.42646 7.70297 4.69529 7.79563 5.04679C7.88847 5.38879 7.86655 5.7178 7.72988 6.0338C7.59322 6.34963 7.37055 6.59304 7.06188 6.76404C6.17572 7.24304 5.45922 7.92863 4.91238 8.82079C4.36572 9.71279 4.09238 10.7019 4.09238 11.788ZM13.1206 20.5228C12.1648 20.5228 11.3529 20.1888 10.6849 19.5208C10.0167 18.8526 9.68263 18.0406 9.68263 17.0848C9.68263 16.2 9.97172 15.4395 10.5499 14.8033C11.1282 14.1671 11.8456 13.804 12.7021 13.714C13.042 13.0952 13.5099 12.596 14.1059 12.2163C14.7019 11.8366 15.3735 11.6468 16.1206 11.6468C17.1076 11.6468 17.9573 11.9437 18.6696 12.5375C19.382 13.1314 19.8 13.8485 19.9239 14.689C20.6522 14.7645 21.2735 15.0651 21.7876 15.5908C22.3016 16.1165 22.5586 16.7728 22.5586 17.5598C22.5586 18.3728 22.2722 19.0699 21.6994 19.651C21.1266 20.2322 20.4336 20.5228 19.6206 20.5228H13.1206ZM13.7109 7.66304V8.66079C13.7109 8.97246 13.6056 9.23354 13.3951 9.44404C13.1846 9.65454 12.9236 9.75979 12.6119 9.75979C12.3004 9.75979 12.037 9.65221 11.8216 9.43704C11.6065 9.22171 11.4989 8.95829 11.4989 8.64679V4.80229C11.4989 4.43413 11.6277 4.12121 11.8854 3.86355C12.1429 3.60605 12.4557 3.47729 12.8239 3.47729H16.6684C16.98 3.47729 17.2411 3.58255 17.4516 3.79305C17.6621 4.00355 17.7674 4.26455 17.7674 4.57605C17.7674 4.88771 17.6621 5.15113 17.4516 5.36629C17.2411 5.58146 16.98 5.68904 16.6684 5.68904H15.4946C15.9786 6.14421 16.4012 6.64421 16.7624 7.18904C17.1237 7.73404 17.4127 8.31671 17.6294 8.93704C17.7597 9.27904 17.7287 9.60313 17.5364 9.90929C17.344 10.2155 17.0638 10.4076 16.6956 10.4858C16.3275 10.5641 15.9909 10.5053 15.6859 10.3093C15.3807 10.1133 15.1452 9.84429 14.9794 9.50229C14.8315 9.14129 14.6489 8.80663 14.4314 8.49829C14.214 8.18996 13.9739 7.91154 13.7109 7.66304Z" fill="#EFEFEF"/> </svg>',
    'diag': '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"> <path d="M13.4663 21.1381C11.7675 21.1381 10.327 20.5392 9.14483 19.3413C7.9628 18.1432 7.37179 16.6948 7.37179 14.996V14.7616C6.01356 14.5174 4.88271 13.873 3.97923 12.8283C3.07576 11.7834 2.62402 10.5538 2.62402 9.13938V4.93938C2.62402 4.62924 2.73255 4.36564 2.94961 4.14858C3.16653 3.93166 3.43006 3.8232 3.7402 3.8232H5.59159C5.63132 3.54479 5.75375 3.31496 5.95887 3.1337C6.164 2.95244 6.4097 2.86182 6.69597 2.86182C7.00612 2.86182 7.26965 2.97027 7.48657 3.18719C7.70363 3.40425 7.81215 3.66785 7.81215 3.978V5.90077C7.81215 6.21092 7.70363 6.47445 7.48657 6.69137C7.26965 6.90842 7.00612 7.01695 6.69597 7.01695C6.4097 7.01695 6.164 6.92633 5.95887 6.74507C5.75375 6.56381 5.63132 6.33398 5.59159 6.05556H4.85638V9.13938C4.85638 10.1136 5.20619 10.9366 5.9058 11.6085C6.60528 12.2803 7.4422 12.6162 8.41658 12.6162C9.39081 12.6162 10.2277 12.2803 10.9271 11.6085C11.6268 10.9366 11.9766 10.1136 11.9766 9.13938V6.05556H11.2414C11.2016 6.33398 11.0792 6.56381 10.8741 6.74507C10.6689 6.92633 10.4232 7.01695 10.137 7.01695C9.82682 7.01695 9.56329 6.90842 9.34638 6.69137C9.12932 6.47445 9.02079 6.21092 9.02079 5.90077V3.978C9.02079 3.66785 9.12932 3.40425 9.34638 3.18719C9.56329 2.97027 9.82682 2.86182 10.137 2.86182C10.4232 2.86182 10.6689 2.95244 10.8741 3.1337C11.0792 3.31496 11.2016 3.54479 11.2414 3.8232H13.0927C13.4029 3.8232 13.6664 3.93166 13.8833 4.14858C14.1004 4.36564 14.2089 4.62924 14.2089 4.93938V9.13938C14.2089 10.53 13.7711 11.7438 12.8954 12.7807C12.0197 13.8174 10.9226 14.4697 9.60415 14.7378V14.996C9.60415 16.0728 9.97867 16.9937 10.7277 17.7586C11.4766 18.5234 12.3895 18.9057 13.4663 18.9057C14.5431 18.9057 15.454 18.5234 16.1991 17.7586C16.9442 16.9937 17.3167 16.0728 17.3167 14.996V13.6921C16.7776 13.4699 16.3382 13.1149 15.9986 12.6271C15.6588 12.1394 15.4889 11.5861 15.4889 10.9672C15.4889 10.1461 15.7743 9.45016 16.345 8.87929C16.9159 8.30856 17.6119 8.0232 18.4329 8.0232C19.2538 8.0232 19.9497 8.30856 20.5206 8.87929C21.0913 9.45016 21.3767 10.1461 21.3767 10.9672C21.3767 11.5861 21.2068 12.1394 20.867 12.6271C20.5274 13.1149 20.0881 13.4699 19.5491 13.6921V14.996C19.5491 16.6948 18.96 18.1432 17.7817 19.3413C16.6037 20.5392 15.1652 21.1381 13.4663 21.1381Z" fill="#EFEFEF"/> </svg>',
}

# 键名即语义：eye_open / eye_closed 是「自动隐藏窗口」的两态（见下面 menu_icon 的说明）。
MENU_ICON_KEYS = ("refresh", "eye_open", "eye_closed", "update", "diag")

# ---- 品牌标志（2026-10-22）----
# 应用图标（Dock / 窗口 / 包体 icon.icns）用同一份图形，出处是这一枚内嵌 SVG：
# 与顶部按钮、菜单行图标同一族做法，不加 .svg 文件，也就不碰 spec 的 datas、
# bundle_closure_check 的闭包，以及"文件没进包时长什么样"第三件事。
# 路径是**纯填充**、圆角用真圆弧 —— 不走"内缩多边形 + 同色描边"那类写法。
# 描边会不会把形状放大，QtSvg 与 Chrome 的口径不同（本轮实测：同一份描边markup，
# 两个渲染器的外框差到 2 个图形单位），而 icon.icns 由 QtSvg 出图、
# 设计稿由 Chrome 出图，靠描边撑形的话两边会画成两个东西。
# 取色用定稿的浅色配色（浅灰底 #e8ebf0 / 偏橙红 #f40000 / 墨 #14181f），底色不画、留透明。
BRAND_LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="110" height="110" viewBox="-5 -5 110 110">'
    '<path fill="#f40000" d="M29.08 44.52A1 1 0 0 1 28.61 45.7L32.81 58.95A1 1 0 0 1 34.23 59.53L72.08 39.14A1 1 0 0 1 72.56 37.96L68.36 24.71A1 1 0 0 1 66.93 24.13L29.08 44.52Z"/>'
    '<path fill="#f40000" d="M91 18.94A1 1 0 0 1 91.84 20.59L73.55 41.31A1 1 0 0 1 71.85 40.95L65.97 22.39A1 1 0 0 1 66.83 21.09L91 18.94Z"/>'
    '<path fill="#14181f" d="M38.88 12.03A1 1 0 0 1 39.83 11.36L54.27 11.36A1 1 0 0 1 55.22 12.69L29.3 87.97A1 1 0 0 1 28.35 88.64L13.91 88.64A1 1 0 0 1 12.96 87.31L38.88 12.03Z"/>'
    '<path fill="#14181f" d="M39.24 48.39A1 1 0 0 1 41 48.12L69.48 87.05A1 1 0 0 1 68.68 88.64L47.39 88.64A1 1 0 0 1 46.59 88.23L32.57 69.08A1 1 0 0 1 32.43 68.17L39.24 48.39Z"/>'
    '</svg>'
)

# ---- 应用图标的色块底（2026-10-23，用户要求"跟程序图标一样有个色块底"）----
# 上面的 BRAND_LOGO_SVG 本身仍然不带底（图形文件只有图形）；色块底由 render_app_icon 在出图时
# 先画一块圆角方形、再把标志叠上去。浅色外观用浅灰底、深色外观用深色底，两档配色都是
# 2026-10-22 定稿的那两套（浅：灰 #e8ebf0 / 偏橙红 #f40000 / 墨 #14181f；
# 深：#12151d / 偏橙红 #fe0118 / 浅墨 #f2f4f8）。
APP_ICON_PLATE_HEX = {"light": "#e8ebf0", "dark": "#12151d"}
# 深色档只换两种图形色，形状一个点都不动：按"浅色 hex → 深色 hex"逐字替换。
_BRAND_DARK_SWAP = {"#f40000": "#fe0118", "#14181f": "#f2f4f8"}

# 色块占画布边长的比例：留 ~10% 透明边，和 Dock 里相邻图标的外轮廓同量级。
APP_ICON_PLATE_SCALE = 0.805
# 色块的角形状：超椭圆 |dx/r|^N + |dy/r|^N = r^N，半径 r 占色块边长的比例与指数 N。
# 数值来自实测（2026-10-23，macOS 27.0.1）：对已装 Skyrocket.app 的包体图标取
# NSWorkspace.iconForFile_ 的系统处理图（Dock 启动前显示的就是它），按 1024 像素逐行量左缘，
# 拟合得 N=3、r=0.36 时均方根误差 1.33 像素（普通圆弧最好 4.33 像素，主窗用的
# renderer.smooth_round_rect_path 在 0.225 半径下 40 像素）。运行时程序把自己的图直接交给
# Dock、不再经系统遮罩，所以色块自己的角必须长成系统遮罩的样子，启动前后才一致。
# 不复用 smooth_round_rect_path：那是主窗/玻璃层共用的曲线，改它会连带四处圆角。
APP_ICON_PLATE_CORNER_RADIUS = 0.36
APP_ICON_PLATE_CORNER_EXPONENT = 3.0
APP_ICON_PLATE_CORNER_STEPS = 48      # 每个角的采样段数；再少 1024 像素下会露出折线
# 标志的 SVG 外框占色块边长的比例。实测此时墨迹宽约占色块的 56%（外框 78% × 墨迹/外框 0.71）。
APP_ICON_MARK_IN_PLATE = 0.78
# svg_pixmap 的 content_scale（占整张画布）。包体 icon.icns / icon.png 必须按同一组数出图，
# 否则装机后 Dock 图标（读 icns）与运行时图标（读这里）的大小、底色会不一致且没有任何报错。
APP_ICON_CONTENT_SCALE = APP_ICON_PLATE_SCALE * APP_ICON_MARK_IN_PLATE


def brand_logo_svg(mode="light"):
    """品牌标志 markup：'light' 原样返回，'dark' 把两种图形色换成深色外观那一套。
    未知 mode 一律按浅色处理（宁可浅色，也不要出错图）。"""
    svg = BRAND_LOGO_SVG
    if mode == "dark":
        for light_hex, dark_hex in _BRAND_DARK_SWAP.items():
            svg = svg.replace(light_hex, dark_hex)
    return svg


def icon_mode_from_scheme(scheme):
    """纯函数：Qt 的 ColorScheme → 'dark' | 'light'。Unknown（含离屏平台）按浅色。"""
    return "dark" if scheme == Qt.ColorScheme.Dark else "light"


def system_icon_mode():
    """当前系统外观对应的图标档：'dark' | 'light'。取不到一律 'light'，不向上抛。"""
    try:
        hints = QGuiApplication.styleHints() if QGuiApplication.instance() is not None else None
        if hints is not None:
            return icon_mode_from_scheme(hints.colorScheme())
    except Exception as exc:
        log_swallow("sf_icons.system_icon_mode", exc)
    return "light"


def connect_appearance_changed(slot):
    """系统在浅色/深色外观间切换时调用 slot。返回是否连上了（没有 QGuiApplication 时 False）。
    slot 应是 QObject 的绑定方法：接收方销毁时 Qt 自动断开，不会留下指向已关窗口的回调。"""
    try:
        hints = QGuiApplication.styleHints() if QGuiApplication.instance() is not None else None
        if hints is None:
            return False
        hints.colorSchemeChanged.connect(slot)
        return True
    except Exception as exc:
        log_swallow("sf_icons.connect_appearance_changed", exc)
        return False

_menu_images_cache = None

def menu_images():
    """右键菜单行首图标的位图（{key: QImage|None}，按设备像素出图 + 标 DPR，进程内缓存）。

    必须在 GUI 线程首次调用（QImage 依赖 GUI 系统已初始化，与 button_images 同口径）。
    QtSvg 缺失或渲染失败时对应值为 None，调用方**不放图标** —— 那时菜单回到
    "无图标 + 勾选框"的形状，「自动隐藏窗口」的开关状态仍由勾号表达，
    诚实性不依赖图标（AGENTS §5.4 那条口径）。
    """
    global _menu_images_cache
    if _menu_images_cache is None:
        px = int(round(MENU_ICON_PT * MENU_ICON_DEVICE_RATIO))
        imgs = {}
        for k in MENU_ICON_KEYS:
            img = svg_pixmap(_MENU_ICON_SVG[k], px, content_scale=MENU_ICON_CONTENT_SCALE)
            if img is not None:
                img.setDevicePixelRatio(MENU_ICON_DEVICE_RATIO)
            imgs[k] = img
        _menu_images_cache = imgs
    return _menu_images_cache

def menu_icon(key):
    """右键菜单某一行的 QIcon；取不到图时返回 None，调用方跳过 setIcon（降级不放图）。

    QPixmap 不能在非 GUI 线程构造，所以这里从线程安全的 QImage 现转，
    与 button_pixmaps 同一个理由。
    """
    try:
        img = menu_images().get(key)
        return QIcon(QPixmap.fromImage(img)) if img is not None else None
    except Exception as exc:
        # 菜单图标是"看得见有没有"的东西，失败必须留痕（AGENTS §6 第 9 条）。
        log_swallow("sf_icons.menu_icon", exc)
        return None

def app_icon_plate_path(x, y, side):
    """应用图标色块的轮廓：四角为超椭圆（参数见 APP_ICON_PLATE_CORNER_*），边为直线。"""
    r = side * APP_ICON_PLATE_CORNER_RADIUS
    n = APP_ICON_PLATE_CORNER_EXPONENT
    power = 2.0 / n
    steps = APP_ICON_PLATE_CORNER_STEPS
    # 四个角的圆心，依次是右上、右下、左下、左上；每个角扫 90°，起点沿顺时针衔接。
    corners = ((x + side - r, y + r, -math.pi / 2), (x + side - r, y + side - r, 0.0),
               (x + r, y + side - r, math.pi / 2), (x + r, y + r, math.pi))
    path = QPainterPath()
    first = True
    for cx, cy, start in corners:
        for i in range(steps + 1):
            t = start + (math.pi / 2) * i / steps
            c, s = math.cos(t), math.sin(t)
            px = cx + r * math.copysign(abs(c) ** power, c)
            py = cy + r * math.copysign(abs(s) ** power, s)
            if first:
                path.moveTo(px, py)
                first = False
            else:
                path.lineTo(px, py)
    path.closeSubpath()
    return path


def render_app_icon(mode="light", s=512):
    """按外观档出一张 s×s 的应用图标：圆角方形色块底 + 品牌标志。不落盘、无副作用。

    包体 icon.icns 的各尺寸也用它出图（s 取 16~1024），所以尺寸是参数。
    取不到图的两种情况（这台机器没有 QtSvg 子模块、markup 被判非法）都退回上一版的
    自绘蓝底蜡烛，并留一条 SWALLOWED：图标是用户天天看的东西，缺依赖时宁可旧造型，
    也不能静默没有图标。
    """
    if mode not in APP_ICON_PLATE_HEX:
        mode = "light"
    mark = svg_pixmap(brand_logo_svg(mode), s, content_scale=APP_ICON_CONTENT_SCALE)
    if mark is None:
        log_swallow("sf_icons.make_app_icon.logo", RuntimeError("品牌标志渲染不可用，退回旧自绘图标"))
        return _legacy_app_icon(s)
    img = QImage(s, s, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    side = s * APP_ICON_PLATE_SCALE
    off = (s - side) / 2
    p.fillPath(app_icon_plate_path(off, off, side), QColor(APP_ICON_PLATE_HEX[mode]))
    p.drawImage(0, 0, mark)
    p.end()
    return img


def make_app_icon(mode="light"):
    """生成应用图标。返回 QImage（线程安全，可后台线程绘制），GUI 线程再转 QPixmap/QIcon

    mode='light'|'dark'：浅色外观 / 深色外观各一张，见 render_app_icon。
    只有浅色档会顺带写一份 ICON_PATH（它和包体 icon.png 同款）；深色档不落盘，
    免得两档在同一个文件上来回覆盖。
    """
    img = render_app_icon(mode)
    # 落盘失败不牵连返回值：调用方要的是内存里的 QImage，
    # ICON_PATH 只是顺带写出的副产物，目录不可写时静默跳过。
    if mode != "dark":
        try:
            os.makedirs(os.path.dirname(ICON_PATH), exist_ok=True)
            img.save(ICON_PATH, "PNG")
        except Exception:
            pass
    return img


def _legacy_app_icon(s):
    """上一版（≤v1.4.101）的应用图标：蓝色渐变圆角底板 + 三根白色蜡烛。
    只作为品牌标志渲染不出来时的兜底，不再是默认造型。"""
    img = QImage(s, s, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    inset = int(s * 0.08)
    box = QRectF(inset, inset, s - 2 * inset, s - 2 * inset)
    path = QPainterPath()
    path.addRoundedRect(box, box.width() * 0.235, box.width() * 0.235)
    grad = QLinearGradient(box.topLeft(), box.bottomRight())
    grad.setColorAt(0, QColor("#3977e8"))
    grad.setColorAt(1, QColor("#1749a8"))
    p.fillPath(path, grad)

    body_w = box.width() * 0.075
    wick_pen = QPen(QColor("#ffffff"), int(s * 0.013))
    candles = [
        (0.28, 0.52, 0.66, 0.42, 0.74),
        (0.50, 0.40, 0.55, 0.30, 0.64),
        (0.72, 0.27, 0.42, 0.18, 0.52),
    ]
    for cxr, bt, bb, wt, wb in candles:
        cx = box.left() + box.width() * cxr
        p.setPen(wick_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(cx, box.top() + box.height() * wt),
                   QPointF(cx, box.top() + box.height() * wb))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#ffffff"))
        top = box.top() + box.height() * bt
        hgt = box.height() * (bb - bt)
        p.drawRoundedRect(QRectF(cx - body_w / 2, top, body_w, hgt),
                          body_w * 0.3, body_w * 0.3)
    p.end()
    return img

def svg_pixmap(svg_xml, px, *, content_scale=1.0):
    """内嵌 SVG → 高 DPI QImage（线程安全，GUI 线程再转 QPixmap）。
    px 为像素尺寸（如显示 10pt @2x → 20）。渲染失败返回 None，调用方回退自绘图标。"""
    # QtSvg 是独立子模块且只在函数内 import：放到模块级时，缺 QtSvg 的部署环境
    # 会让整个 sf_icons 导入失败，连自绘兜底图标和 Dock 图标一起陪葬。
    try:
        from PyQt6.QtSvg import QSvgRenderer
        renderer = QSvgRenderer(svg_xml.encode("utf-8"))
        if renderer is None or not renderer.isValid():
            return None
        img = QImage(px, px, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        content_px = px * content_scale
        inset = (px - content_px) / 2
        renderer.render(p, QRectF(inset, inset, content_px, content_px))
        p.end()
        return img
    except Exception:
        return None


# v1.4.11：顶部按钮图标改为「首帧之前同步渲染」。
# 原先由后台线程投递（_icon_worker 第 3 步），窗口先显示、图标后到位 ——
# 首帧只能画自绘兜底造型，SVG 到位后同一位置换成另一套更小的造型：
# 实测 edit/close 墨迹由 13×13px 跳变为 8×8px、min 由 11×2px 跳为 8×2px，
# 铅笔还会突变成齿轮 —— 用户看到的就是"右上角按钮先放大再缩小"。
# 三个 SVG 的渲染成本仅约 5ms（含 QtSvg 首次 import 3.5ms），远低于后台线程
# 里被 AppKit import（约 122ms）+ 512px 应用图标（约 17ms）挡在后面的等待，
# 因此把它挪到 GUI 线程首帧之前，冷启动首帧即为最终形态。
_BUTTON_ICON_KEYS = ("edit", "close", "min")

_button_images_cache = None

# 出图边长与 DPR：12pt 的框在 Retina 上占 24 个设备像素，就按 24 出图并标上 DPR。
_BUTTON_ICON_PX = int(round(BTN_ICON_PT * BTN_ICON_DEVICE_RATIO))

def button_images():
    """顶部三按钮的 SVG 位图（按设备像素出图 + 标 DPR，进程内缓存）。{kind: QImage|None}

    在 GUI 线程首次调用时同步渲染；QtSvg 缺失/渲染失败时对应值为 None，
    调用方沿用自绘兜底图标（降级不白屏）。

    v1.4.59（走查 S8）：这里原来是 `svg_pixmap(svg, 20)` 且不设 devicePixelRatio，
    而绘制框是 12pt —— Retina 上要 24 个设备像素，于是 20→24 是**放大**，图标发虚。
    现在出图 24px 并 setDevicePixelRatio(2.0)：Qt 把它当 12pt 的图，逐像素 1:1 取样。
    判据（差 255/平均 34.9 → 差 0）与实测过程写在 sf_constants.BTN_ICON_DEVICE_RATIO
    附近，能失败的用例在 tests/test_v1459_button_icons.py。
    """
    global _button_images_cache
    if _button_images_cache is None:
        imgs = {}
        for k in _BUTTON_ICON_KEYS:
            img = svg_pixmap(_ICON_SVG[k], _BUTTON_ICON_PX)
            if img is not None:
                img.setDevicePixelRatio(BTN_ICON_DEVICE_RATIO)
            imgs[k] = img
        _button_images_cache = imgs
    return _button_images_cache

def button_pixmaps():
    """顶部三按钮的 QPixmap（供窗口直接绘制）。必须在 GUI 线程调用。

    QPixmap 不能在非 GUI 线程构造，故把线程安全的 QImage 在这里转换；
    渲染失败的项为 None，窗口沿用自绘兜底。
    """
    try:
        imgs = button_images()
        return {k: (QPixmap.fromImage(imgs[k]) if imgs[k] is not None else None)
                for k in _BUTTON_ICON_KEYS}
    except Exception as exc:
        # 极端环境（无 GUI 系统 / Qt 初始化异常）下不能让建窗失败：
        # 全部退回 None，绘制侧沿用自绘兜底图标。
        log_line("button icons sync render failed: %r" % (exc,), level="WARNING")
        return {k: None for k in _BUTTON_ICON_KEYS}

def hide_dock():
    """隐藏 Dock 程序坞图标（accessory 模式，不出现在 Dock 与 Cmd+Tab）"""
    try:
        from AppKit import NSApp, NSApplicationActivationPolicyAccessory
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    except Exception:
        pass

def show_dock():
    """显示 Dock 程序坞图标（regular 模式，出现在 Dock 与 Cmd+Tab）"""
    try:
        from AppKit import NSApp, NSApplicationActivationPolicyRegular
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        log_line("show_dock OK, policy=%r" % NSApp.activationPolicy())
    except Exception as e:
        log_line("show_dock ERROR: %r" % e)

def sf_symbol_pixmap(name, size, color, scale=2, weight=None):
    """从 macOS SF Symbols（苹果图标库）获取图标并转为着色 QImage。

    用 NSGraphicsContext 渲染 scale 倍分辨率，保证缩放到 size 时依然清晰。
    weight 传入 NSFontWeight* 常量可控制图标粗细（如 NSFontWeightBold 加粗）。
    失败时返回 None，调用方回退到自绘图标。返回 QImage（线程安全，GUI 线程再转 QPixmap）。
    """
    try:
        from AppKit import (NSImage, NSBitmapImageRep, NSGraphicsContext,
                            NSMakeRect, NSImageSymbolConfiguration)
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
        if img is None:
            return None
        if weight is not None:
            cfg = NSImageSymbolConfiguration.configurationWithPointSize_weight_(size, weight)
            img = img.imageWithSymbolConfiguration_(cfg)
        px = size * scale
        rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, px, px, 8, 4, True, False, "NSCalibratedRGBColorSpace", 0, 0)
        ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
        # 第1轮走查修复：save/restore 用 try/finally 配对 —— drawInRect_ 招异常时
        # 原实现会跳过 restore，当前线程 graphics state 栈永久残留一次 save。
        NSGraphicsContext.saveGraphicsState()
        try:
            NSGraphicsContext.setCurrentContext_(ctx)
            img.drawInRect_(NSMakeRect(0, 0, px, px))
        finally:
            NSGraphicsContext.restoreGraphicsState()
        w, h = rep.pixelsWide(), rep.pixelsHigh()
        bpr = rep.bytesPerRow()
        # 必须 .copy()：这个 QImage 直接引用 AppKit NSBitmapImageRep 的缓冲区，
        # rep 释放后指针即失效，后续上色/绘制会读到已回收内存（偶发花图或崩溃）。
        qimg = QImage(rep.bitmapData(), w, h, bpr, QImage.Format.Format_RGBA8888).copy()
        tinted = QImage(qimg.size(), QImage.Format.Format_ARGB32)
        tinted.fill(Qt.GlobalColor.transparent)
        p = QPainter(tinted)
        p.drawImage(0, 0, qimg)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        p.fillRect(tinted.rect(), color)
        p.end()
        return tinted
    except Exception:
        return None

# S-3 v1.2.50：图标队列改为有界（容量 8）—— 防止消费端异常停摆时 QImage 无限堆积。
# v1.4.11：顶部按钮图标不再走本队列（改为首帧前同步渲染，见 button_images），
# 当前生产量仅 1 项（app_icon）。
_ICON_QUEUE = queue.Queue(maxsize=8)

_appkit_ready = threading.Event()   # AppKit import 完成标志

# 与主窗启动流程的双向握手：本线程预热完 AppKit 后 set _appkit_ready，主线程
# 构造完 QApplication 后 set _app_ready —— QImage/QPainter 在后者就绪前不可用。
_app_ready = threading.Event()      # QApplication 构造完成标志

def _objc_deps():
    """缓存 pyobjc 依赖的 import（首次调用耗时，后续复用）"""
    global _objc_deps_cache
    if _objc_deps_cache is None:
        import objc
        from ctypes import c_void_p
        _objc_deps_cache = (objc, c_void_p)
    return _objc_deps_cache

_objc_deps_cache = None

def _icon_worker():
    # 1) 预热 AppKit（首次 import 约数百 ms，与主线程 QApplication 构造并行）
    #    预热的效果就是这条 import 本身：这里原来还有一句把 NSFontWeightBold 赋给
    #    `weight`、赋完从没读过的赋值，留着只会让人以为图标字重与它有关。
    try:
        from AppKit import NSFontWeightBold, NSImage  # noqa: F401
    except Exception:
        pass
    _appkit_ready.set()
    # 2) 等待 QApplication 就绪后再绘制应用图标（QImage 依赖 GUI 系统已初始化）
    # v1.2.48(H3)：区分超时与真实失败 —— _app_ready.wait 返回 False 表示 10s 内
    # GUI 未就绪（极慢启动/系统忙），此时 make_app_icon 必然异常；超时则跳过应用
    # 图标生成，不把 None 入队伪装成"生成失败"导致图标静默缺失；按钮图标不受影响。
    if _app_ready.wait(timeout=10):
        # 浅、深两档一次出完：切换外观时主线程只需换图，不在 GUI 线程上重新渲染。
        # 两档各自成败，一档失败不拖另一档（失败以 None 占位，消费端沿用上一版）。
        for kind, mode in (("app_icon", "light"), ("app_icon_dark", "dark")):
            try:
                _ICON_QUEUE.put((kind, make_app_icon(mode)))
            except Exception:
                _ICON_QUEUE.put((kind, None))
    # v1.4.11：原先此处还在后台渲染顶部三个按钮 SVG 并入队，已挪到 GUI 线程
    # 首帧之前（button_images）—— 后台投递会让首帧先画一版放大的自绘兜底，
    # 图标到位后再缩回 SVG 造型，即用户报告的"启动时右上按钮突然放大又缩小"。

def start_icon_worker():
    """拉起图标后台线程（主窗启动路径调用）。

    daemon=True 是有意的：图标只是渐进增强，绝不能反过来拖住退出 —— worker 在
    `_app_ready.wait(timeout=10)` 上最多挂 10 秒，若是非守护线程，GUI 一直没就绪时
    退出流程会被它一起卡住。按钮图标不走这条后台路（v1.4.11 起挪到首帧之前绘制），
    这里只负责应用图标。
    """
    # daemon：图标只是渐进增强，绝不能拖住退出 —— 主窗未 set _app_ready 时
    # worker 最多还要在 wait 上挂 10s，非守护线程会把退出流程一起卡住。
    threading.Thread(target=_icon_worker, daemon=True).start()
