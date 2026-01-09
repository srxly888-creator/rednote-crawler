class XHSCrawlerException(Exception):
    """Base exception for XHS Crawler. / 小红书爬虫的基础异常。"""
    pass

class CaptchaDetectedException(XHSCrawlerException):
    """Raised when a captcha is detected during crawling. / 爬取过程中检测到验证码时抛出。"""
    pass

class LoginRequiredException(XHSCrawlerException):
    """Raised when login is required but session is invalid. / 需要登录但会话无效时抛出。"""
    pass

class EndOfResultsException(XHSCrawlerException):
    """Raised when the end of search results is reached. / 当到达搜索结果末尾时抛出。"""
    pass
