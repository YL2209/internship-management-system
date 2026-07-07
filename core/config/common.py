


# 小程序版本号
XCX_VERSION = "1.7.10"  # 可通过 config.xcx_version 覆盖

# 签名密钥
XCX_KEY = "ZsE4rGnjI9PkHqAz2WseDc4RF8Uh7YgVMb5Ke48NemJ4saA6XcQ821fFT061pC"

# 微信小程序 AppID
XCX_APP_ID = "wx9f1c2e0bbc10673c"

# 小程序的 Referer 标识
XCX_REFERER_ID = "587"
XCX_REFERER = (
    "https://servicewechat.com/"
    + XCX_APP_ID + "/"
    + XCX_REFERER_ID + "/page-frame.html"
)

# 签名计算时需要排除的字段名列表（这些字段值不参与 MD5 计算）
XCX_EXCLUDED_KEYS = [
    "content", "deviceName", "keyWord", "blogBody", "blogTitle", "getType",
    "responsibilities", "street", "text", "reason", "searchvalue", "key",
    "answers", "leaveReason", "personRemark", "selfAppraisal", "imgUrl",
    "wxname", "deviceId", "avatarTempPath", "file", "model", "brand", "system",
    "deviceId", "platform", "code", "openId", "unionid", "clockDeviceToken",
    "clockDevice", "address", "name", "enterpriseEmail", "responsibilities",
    "practiceTarget", "guardianName", "guardianPhone", "practiceDays", "linkman",
    "enterpriseName", "companyIntroduction", "accommodationStreet",
    "accommodationLongitude", "accommodationLatitude", "internshipDestination",
    "specialStatement", "enterpriseStreet", "insuranceName", "insuranceFinancing",
    "policyNumber", "overtimeRemark", "riskStatement", "specialStatement",
    "unionId"
]

# 签名头 n 字段的值 = 所有排除字段名的逗号拼接
XCX_N_HEADER = ",".join(XCX_EXCLUDED_KEYS)

# 高德地图逆地理编码 Web API Key（内置默认值，可被 config.mapApiKeys.amap 覆盖）
AMAP_WEB_KEY = "c222383ff12d31b556c3ad6145bb95f4"

# 腾讯地图逆地理编码 Key（内置默认值，可被 config.mapApiKeys.tencent 覆盖）
TENCENT_MAP_KEY = "GOZBZ-E4L67-6WLXT-PSLBH-2WEZZ-LOFLE"

# SM2 国密加密公钥（用于生成 devicecode 请求头，加密设备指纹）
XCX_SM2_PUBLIC_KEY = "04a3c35de075a2e86f28d52a41989a08e740a82fb96d43d9af8a5509e0a4e837ecb384c44fe1ee95f601ef36f3c892214d45c9b3f75b57556466876ad6052f0f1f"
XCX_SM2_MODE = 1

